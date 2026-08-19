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


SEMANTIC_RELEVANCE_MIN = 0.64
SEMANTIC_RELATIVE_MIN = 0.89
HYBRID_SEMANTIC_MIN = 0.46
HYBRID_LEXICAL_RATIO_MIN = 0.25
LOCAL_EMBEDDING_BATCH_SIZE = 8


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
                        [self.embedding_text(record) for record in active],
                        batch_size=LOCAL_EMBEDDING_BATCH_SIZE,
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
                except ModuleNotFoundError:
                    embedding_status = "disabled:dependency-not-installed"
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

    def health(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Report lexical and semantic index readiness against authority data."""
        active_count = sum(
            1 for record in records if record.get("status") == "active"
        )
        result: dict[str, Any] = {
            "integrity": "missing",
            "active_records": active_count,
            "indexed_records": 0,
            "vector_records": 0,
            "embedding_model": "",
            "embedding_status": "unknown",
            "fingerprint_matches": False,
            "index_ready": False,
            "semantic_ready": False,
            "mode": "lexical-only",
            "error": "",
        }
        try:
            with sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
            ) as conn:
                result["integrity"] = str(
                    conn.execute("PRAGMA integrity_check").fetchone()[0]
                )
                result["indexed_records"] = int(
                    conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                )
                result["vector_records"] = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memory_vectors"
                    ).fetchone()[0]
                )
                metadata = dict(
                    conn.execute(
                        "SELECT key,value FROM index_meta"
                    ).fetchall()
                )
        except (OSError, sqlite3.Error) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        result["embedding_model"] = metadata.get("embedding_model", "")
        result["embedding_status"] = metadata.get(
            "embedding_status", "unknown"
        )
        result["fingerprint_matches"] = (
            metadata.get("records_fingerprint")
            == self.active_records_fingerprint(records)
        )
        result["index_ready"] = bool(
            result["integrity"] == "ok"
            and metadata.get("schema_version") == str(self.schema_version)
            and result["fingerprint_matches"]
            and result["indexed_records"] == active_count
        )
        result["semantic_ready"] = bool(
            result["index_ready"]
            and (
                active_count == 0
                or (
                    result["embedding_status"] == "ready"
                    and result["embedding_model"] == self.embedding_model
                    and result["vector_records"] == active_count
                )
            )
        )
        if result["semantic_ready"]:
            result["mode"] = "local-hybrid"
        return result

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
                   m.search_tokens,
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
            "updated_at", "search_tokens", "rank",
        )
        return [dict(zip(columns, row)) for row in rows]

    def lexical_match_is_relevant(
        self,
        query: str,
        record: dict[str, Any],
    ) -> bool:
        """Require multiple lexical signals when semantic search is absent.

        FTS uses an OR expression so that natural phrasing can still find a
        durable fact, but one shared place name, number, or generic word is not
        enough evidence to inject memory into an unrelated turn.
        """
        query_terms = {
            term for term in self.search_terms(query)
            if not term.isdigit()
        }
        raw_text = " ".join(
            str(record.get(field, ""))
            for field in ("title", "summary", "body", "tags", "topic")
        ).lower()
        indexed_terms = set(
            str(record.get("search_tokens", "")).lower().split()
        )
        indexed_terms.update(self.search_terms(raw_text))
        overlap = query_terms & indexed_terms
        if len(overlap) >= 3:
            return True

        for term in query_terms:
            if term not in raw_text:
                continue
            if re.fullmatch(r"[a-z0-9_./~-]{4,}", term):
                return True
            if len(term) >= 3 and re.fullmatch(r"[\u4e00-\u9fff]+", term):
                return True
        return False

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
                # Require at least one non-digit query term to appear in the
                # record's search tokens.  This prevents spurious FTS matches
                # where the only overlap comes from digit tokens (e.g. "19"
                # matching inside a date like "2026-08-19") from passing the
                # corroborated filter alongside a weak vector score.
                _query_terms_nd = {
                    t for t in self.search_terms(query) if not t.isdigit()
                }
                _rec_tokens = set(
                    str(lexical_by_id.get(
                        record_id, {}
                    ).get("search_tokens", "")).lower().split()
                )
                _has_meaningful_lexical = bool(
                    _query_terms_nd & _rec_tokens
                )
                corroborated = (
                    record_id in lexical_by_id
                    and record_id in semantic_by_id
                    and semantic_score >= HYBRID_SEMANTIC_MIN
                    and best_lexical_strength > 0
                    and lexical_strength / best_lexical_strength
                    >= HYBRID_LEXICAL_RATIO_MIN
                    and _has_meaningful_lexical
                )
                lexical_only = (
                    not semantic
                    and record_id in lexical_by_id
                    and self.lexical_match_is_relevant(
                        query, lexical_by_id[record_id]
                    )
                )
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
