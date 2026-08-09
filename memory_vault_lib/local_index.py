"""Authority-aligned FTS and vector index for the local memory vault."""

from __future__ import annotations

import array
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, ContextManager, Sequence


SEMANTIC_RELEVANCE_MIN = 0.58
SEMANTIC_RELATIVE_MIN = 0.85
HYBRID_SEMANTIC_MIN = 0.46
HYBRID_LEXICAL_RATIO_MIN = 0.25


class LocalSearchIndex:
    """Build and query a rebuildable index without owning authoritative data."""

    def __init__(
        self,
        *,
        path: Path,
        schema_version: int,
        embedding_model: str,
        alias_groups: Sequence[tuple[set[str], set[str]]],
        embedder_factory: Callable[[], Any],
        ensure_layout: Callable[[], None],
        lock_factory: Callable[[], ContextManager[Any]],
    ) -> None:
        self.path = path
        self.schema_version = schema_version
        self.embedding_model = embedding_model
        self.alias_groups = alias_groups
        self.embedder_factory = embedder_factory
        self.ensure_layout = ensure_layout
        self.lock_factory = lock_factory

    @staticmethod
    def active_records_fingerprint(records: list[dict[str, Any]]) -> str:
        rows = sorted(
            (
                str(record.get("id", "")),
                str(record.get("content_hash", "")),
                str(record.get("updated_at", "")),
            )
            for record in records
            if record.get("status") == "active"
        )
        encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def search_terms(self, text: str) -> list[str]:
        lowered = re.sub(r"\s+", " ", text or "").strip().lower()
        terms = set(re.findall(r"[a-z0-9_./~-]{2,}", lowered))
        for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
            terms.add(sequence)
            for size in (2, 3):
                terms.update(
                    sequence[index:index + size]
                    for index in range(max(0, len(sequence) - size + 1))
                )
        for triggers, aliases in self.alias_groups:
            if any(trigger in lowered for trigger in triggers):
                terms.update(aliases)
        return sorted(term for term in terms if term)

    def embedding_text(self, record: dict[str, Any]) -> str:
        aliases = " ".join(self.search_terms(
            " ".join((
                str(record.get("title", "")),
                str(record.get("summary", "")),
                str(record.get("body", "")),
            ))
        ))
        return "\n".join((
            str(record.get("title", "")),
            str(record.get("summary", "")),
            str(record.get("body", "")),
            aliases,
        ))

    @staticmethod
    def normalized_vector(values: Any) -> array.array:
        vector = array.array("f", (float(value) for value in values))
        magnitude = sum(value * value for value in vector) ** 0.5
        if magnitude:
            for index in range(len(vector)):
                vector[index] /= magnitude
        return vector

    def build(self, records: list[dict[str, Any]], target: Path) -> None:
        active = [
            record for record in records
            if record.get("status") == "active"
        ]
        rows = [
            (
                str(record.get("id", "")),
                str(record.get("topic", "other")),
                str(record.get("title", "")),
                str(record.get("summary", "")),
                str(record.get("body", "")),
                " ".join(str(tag) for tag in record.get("tags", [])),
                str(record.get("updated_at", "")),
                " ".join(self.search_terms(self.embedding_text(record))),
                str(record.get("content_hash", "")),
            )
            for record in active
        ]
        with sqlite3.connect(target) as conn:
            conn.executescript(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, topic TEXT, title TEXT, summary TEXT,
                    body TEXT, tags TEXT, updated_at TEXT, search_tokens TEXT,
                    content_hash TEXT
                );
                CREATE INDEX memories_topic_idx ON memories(topic);
                CREATE VIRTUAL TABLE memories_fts USING fts5(
                    id UNINDEXED, title, summary, body, tags, topic, search_tokens,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE memory_vectors (
                    id TEXT PRIMARY KEY, model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL, vector BLOB NOT NULL
                );
                CREATE TABLE index_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO memories_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (row[0], row[2], row[3], row[4], row[5], row[1], row[7])
                    for row in rows
                ],
            )
            embedding_status = "unavailable"
            if active:
                try:
                    vectors = self.embedder_factory().embed(
                        [self.embedding_text(record) for record in active]
                    )
                    vector_rows = []
                    for record, values in zip(active, vectors):
                        vector = self.normalized_vector(values)
                        vector_rows.append(
                            (
                                str(record["id"]),
                                self.embedding_model,
                                len(vector),
                                vector.tobytes(),
                            )
                        )
                    conn.executemany(
                        """
                        INSERT INTO memory_vectors(id,model,dimensions,vector)
                        VALUES(?,?,?,?)
                        """,
                        vector_rows,
                    )
                    embedding_status = "ready"
                except Exception as exc:
                    embedding_status = f"unavailable:{type(exc).__name__}"
            metadata = {
                "schema_version": str(self.schema_version),
                "records_fingerprint": self.active_records_fingerprint(records),
                "embedding_model": self.embedding_model,
                "embedding_status": embedding_status,
            }
            conn.executemany(
                "INSERT INTO index_meta(key,value) VALUES(?,?)",
                list(metadata.items()),
            )
            conn.commit()

    def rebuild(self, records: list[dict[str, Any]]) -> None:
        """Atomically rebuild the index under the authority-store lock."""
        self.ensure_layout()
        with self.lock_factory():
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}."
                f"{uuid.uuid4().hex}.tmp"
            )
            try:
                self.build(records, tmp)
                os.replace(tmp, self.path)
            finally:
                tmp.unlink(missing_ok=True)

    def ensure(self, records: list[dict[str, Any]]) -> bool:
        expected = self.active_records_fingerprint(records)
        try:
            with sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
            ) as conn:
                metadata = dict(
                    conn.execute(
                        "SELECT key,value FROM index_meta"
                    ).fetchall()
                )
            if (
                metadata.get("schema_version") == str(self.schema_version)
                and metadata.get("records_fingerprint") == expected
            ):
                return False
        except (OSError, sqlite3.Error):
            pass
        self.rebuild(records)
        return True

    def fts_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        top_k: int,
    ) -> list[dict[str, Any]]:
        terms = self.search_terms(query)
        if not terms:
            return []
        expression = " OR ".join(
            '"' + term.replace('"', '""') + '"' for term in terms
        )
        rows = conn.execute(
            """
            SELECT m.id,m.topic,m.title,m.summary,m.body,m.tags,m.updated_at,
                   bm25(
                       memories_fts,0.0,8.0,4.0,1.0,1.5,1.5,2.0
                   ) AS rank
            FROM memories_fts
            JOIN memories AS m ON m.id=memories_fts.id
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (expression, top_k),
        ).fetchall()
        columns = (
            "id", "topic", "title", "summary", "body", "tags",
            "updated_at", "rank",
        )
        return [dict(zip(columns, row)) for row in rows]

    def vector_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        top_k: int,
    ) -> list[dict[str, Any]]:
        try:
            query_vector = self.normalized_vector(
                next(iter(self.embedder_factory().query_embed([query])))
            )
        except Exception:
            return []
        rows = conn.execute(
            """
            SELECT m.id,m.topic,m.title,m.summary,m.body,m.tags,m.updated_at,
                   v.dimensions,v.vector
            FROM memory_vectors AS v
            JOIN memories AS m ON m.id=v.id
            WHERE v.model=?
            """,
            (self.embedding_model,),
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            dimensions = int(row[7])
            vector = array.array("f")
            vector.frombytes(row[8])
            if dimensions != len(vector) or len(query_vector) != dimensions:
                continue
            score = sum(
                left * right
                for left, right in zip(query_vector, vector)
            )
            record = dict(zip(
                (
                    "id", "topic", "title", "summary", "body", "tags",
                    "updated_at",
                ),
                row[:7],
            ))
            record["semantic_score"] = round(score, 6)
            scored.append((score, record))
        scored.sort(
            key=lambda item: (item[0], item[1]["updated_at"]),
            reverse=True,
        )
        return [record for _, record in scored[:top_k]]

    def search(
        self,
        records: list[dict[str, Any]],
        query: str,
        *,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        self.ensure(records)
        requested = max(1, min(top_k, 50))
        with sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
        ) as conn:
            lexical = self.fts_search(
                conn,
                query,
                top_k=min(50, requested * 3),
            )
            semantic = self.vector_search(
                conn,
                query,
                top_k=min(50, requested * 3),
            )
        scores: dict[str, float] = {}
        by_id: dict[str, dict[str, Any]] = {}
        lexical_by_id = {
            str(record["id"]): record
            for record in lexical
        }
        semantic_by_id = {
            str(record["id"]): record
            for record in semantic
        }
        best_lexical_strength = max(
            (
                abs(float(record.get("rank", 0.0)))
                for record in lexical
            ),
            default=0.0,
        )
        best_semantic_score = max(
            (
                float(record.get("semantic_score", 0.0))
                for record in semantic
            ),
            default=0.0,
        )
        for weight, rows in ((1.25, lexical), (1.0, semantic)):
            for rank, record in enumerate(rows, 1):
                record_id = str(record["id"])
                semantic_score = float(
                    semantic_by_id.get(record_id, {}).get(
                        "semantic_score",
                        0.0,
                    )
                )
                lexical_strength = abs(float(
                    lexical_by_id.get(record_id, {}).get("rank", 0.0)
                ))
                strong_semantic = (
                    semantic_score >= SEMANTIC_RELEVANCE_MIN
                    and best_semantic_score > 0
                    and semantic_score / best_semantic_score
                    >= SEMANTIC_RELATIVE_MIN
                )
                corroborated = (
                    record_id in lexical_by_id
                    and record_id in semantic_by_id
                    and semantic_score >= HYBRID_SEMANTIC_MIN
                    and best_lexical_strength > 0
                    and lexical_strength / best_lexical_strength
                    >= HYBRID_LEXICAL_RATIO_MIN
                )
                # Local embeddings are optional.  When the embedding backend
                # is unavailable, an exact/keyword FTS hit is still durable
                # evidence and must not be discarded solely because no vector
                # row can corroborate it.  Keep the stricter hybrid gate when
                # semantic candidates are present.
                lexical_only = not semantic and record_id in lexical_by_id
                if not (strong_semantic or corroborated or lexical_only):
                    continue
                scores[record_id] = (
                    scores.get(record_id, 0.0) + weight / (60 + rank)
                )
                by_id[record_id] = record
        ordered = sorted(
            by_id,
            key=lambda record_id: (
                scores[record_id],
                str(by_id[record_id].get("updated_at", "")),
                record_id,
            ),
            reverse=True,
        )
        results = []
        for record_id in ordered[:requested]:
            record = dict(by_id[record_id])
            record["score"] = round(scores[record_id], 6)
            results.append(record)
        return results
