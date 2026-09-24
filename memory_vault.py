#!/usr/bin/env python3
"""Local long-term memory vault for Max's Hermes install.

This keeps MEMORY.md / USER.md small by moving durable but non-core facts into
a local authority store. The vault is the complete production memory path:
writes, SQLite/FTS5 retrieval, and local embeddings stay on this machine.

The old mem0/Qdrant export and replay functions remain below only as dormant
retirement/recovery code; normal Vault writes never enqueue or synchronize them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_vault_lib import (

    LocalSearchIndex,
    MemoryGovernance,
    SecretMemoryRejected,
)


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
MEMORIES_DIR = HERMES_HOME / "memories"
VAULT_DIR = HERMES_HOME / "memory-vault"
TOPICS_DIR = VAULT_DIR / "topics"
REPORT_DIR = HERMES_HOME / "reports" / "memory-audit"
GOVERNANCE_REPORT_DIR = HERMES_HOME / "reports" / "memory-governance"

RECORDS_PATH = VAULT_DIR / "memories.jsonl"
HISTORY_PATH = VAULT_DIR / "history.jsonl"
EVIDENCE_PATH = VAULT_DIR / "evidence.jsonl"
INDEX_PATH = VAULT_DIR / "index.json"
OUTBOX_PATH = VAULT_DIR / "index-outbox.jsonl"
OUTBOX_STATE_PATH = VAULT_DIR / "index-outbox-state.json"
LOCAL_INDEX_PATH = VAULT_DIR / "local-search.sqlite3"
LOCAL_EMBEDDING_CACHE = HERMES_HOME / "cache" / "fastembed"
VAULT_DB_PATH = VAULT_DIR / "vault.sqlite3"
VAULT_LOCK_PATH = VAULT_DIR / "vault.lock"
README_PATH = VAULT_DIR / "README.md"
MEM0_SEED_PATH = VAULT_DIR / "mem0_seed.jsonl"
HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR", str(HERMES_HOME / "hermes-agent"))
).expanduser()
HERMES_PYTHON = HERMES_AGENT_DIR / ".venv" / "bin" / "python"


def configure_home(home: str | Path) -> None:
    """Bind this isolated vault module to one durable storage home.

    The Gateway multiplexes user profiles in one process.  Each provider gets
    its own imported module, so rebinding these module globals keeps records,
    SQLite, and the local index profile-scoped without mutating process-wide
    environment variables.
    """
    global HERMES_HOME, MEMORIES_DIR, VAULT_DIR, TOPICS_DIR, REPORT_DIR
    global GOVERNANCE_REPORT_DIR, RECORDS_PATH, HISTORY_PATH, EVIDENCE_PATH, INDEX_PATH
    global OUTBOX_PATH, OUTBOX_STATE_PATH, LOCAL_INDEX_PATH
    global LOCAL_EMBEDDING_CACHE, VAULT_DB_PATH, VAULT_LOCK_PATH
    global README_PATH, MEM0_SEED_PATH, _LOCAL_EMBEDDER

    HERMES_HOME = Path(home).expanduser()
    MEMORIES_DIR = HERMES_HOME / "memories"
    VAULT_DIR = HERMES_HOME / "memory-vault"
    TOPICS_DIR = VAULT_DIR / "topics"
    REPORT_DIR = HERMES_HOME / "reports" / "memory-audit"
    GOVERNANCE_REPORT_DIR = HERMES_HOME / "reports" / "memory-governance"
    RECORDS_PATH = VAULT_DIR / "memories.jsonl"
    HISTORY_PATH = VAULT_DIR / "history.jsonl"
    EVIDENCE_PATH = VAULT_DIR / "evidence.jsonl"
    INDEX_PATH = VAULT_DIR / "index.json"
    OUTBOX_PATH = VAULT_DIR / "index-outbox.jsonl"
    OUTBOX_STATE_PATH = VAULT_DIR / "index-outbox-state.json"
    LOCAL_INDEX_PATH = VAULT_DIR / "local-search.sqlite3"
    LOCAL_EMBEDDING_CACHE = HERMES_HOME / "cache" / "fastembed"
    VAULT_DB_PATH = VAULT_DIR / "vault.sqlite3"
    VAULT_LOCK_PATH = VAULT_DIR / "vault.lock"
    README_PATH = VAULT_DIR / "README.md"
    MEM0_SEED_PATH = VAULT_DIR / "mem0_seed.jsonl"
    _LOCAL_EMBEDDER = None

ENTRY_DELIMITER = "\n§\n"
SCHEMA_VERSION = 2
CURRENT_SUPPORTED_SCHEMA = SCHEMA_VERSION
LOCAL_INDEX_SCHEMA_VERSION = 3
LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_EMBEDDING_REVISION = ""  # set when a pinned model revision is known

try:
    import fcntl
except ImportError:  # pragma: no cover - Hermes local deployment is POSIX
    fcntl = None

_PROCESS_LOCK = threading.RLock()
_TX_LOCAL = threading.local()
_EMBEDDER_LOCK = threading.Lock()
_LOCAL_EMBEDDER: Any = None

TOPIC_LABELS = {
    "profile": "User profile and communication preferences",
    "workflow": "Workflows, verification rules, and agent behavior",
    "hermes_ops": "Hermes, Codex, MCP, TTS, Home Assistant, and automation",
    "health": "Health, medication, body metrics, and care plans",
    "family": "Family, baby, spouse, household, and childcare",
    "company_finance": "Company, legal, finance, tax, and holdings",
    "products": "Product decisions, device preferences, and purchasing rules",
    "audio": "Audio gear and listening preferences",
    "travel": "Travel wishlist and destination preferences",
    "astrology": "Astrology and Chinese-metaphysics expert context",
    "gaming": "Games and entertainment preferences",
    "other": "Other durable facts",
}

CORE_KEEP_TOPICS = {"profile", "workflow", "hermes_ops"}
VALID_STATUSES = {"pending", "active", "superseded", "archived", "disputed", "rejected"}
SEARCH_ALIAS_GROUPS = (
    (
        {"home assistant", "ha_control", "ha_set_temp", "空调", "冷气", "冷气机", "调温"},
        {
            "home", "assistant", "ha", "ha_control.py", "ha_set_temp.py",
            "空调", "冷气", "冷气机", "调温", "温度", "家里",
        },
    ),
    (
        {"护肤", "刺痛", "屏障", "阿达帕林", "壬二酸"},
        {"护肤", "刺痛", "屏障", "护理", "早晚", "阿达帕林", "壬二酸", "防晒"},
    ),
    (
        {"apple music", "苹果音乐", "无损", "aac"},
        {"apple", "music", "苹果音乐", "无损", "音质", "重开", "重置", "aac", "windows"},
    ),
    (
        {"特斯拉", "tesla", "开什么车", "什么车", "车型", "座驾", "车辆"},
        {"特斯拉", "tesla", "汽车", "车", "车型", "座驾", "车辆", "驾驶"},
    ),
    (
        {"唯一推荐", "明确理由", "不要模棱两可", "直接结论"},
        {"唯一", "推荐", "明确", "理由", "直接", "结论", "可能性", "模棱两可", "回答"},
    ),
)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_hermes_runtime() -> None:
    if os.environ.get("MEMORY_VAULT_NO_REEXEC"):
        return
    if not HERMES_PYTHON.exists():
        return
    try:
        current_prefix = Path(sys.prefix).resolve()
        target_prefix = HERMES_PYTHON.parent.parent.resolve()
    except OSError:
        return
    if current_prefix == target_prefix:
        return
    env = dict(os.environ)
    env["MEMORY_VAULT_NO_REEXEC"] = "1"
    target = HERMES_PYTHON
    os.execve(str(target), [str(target), __file__, *sys.argv[1:]], env)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip().lower())
    text = re.sub(r"-+", "-", text).strip("-_")
    return text or "other"


def stable_id(source: str, title: str) -> str:
    key = f"{source}:{normalize(title).lower()}"
    return "mem_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def history_id() -> str:
    raw = f"{dt.datetime.now().timestamp():.9f}:{os.getpid()}:{threading.get_ident()}"
    return "hist_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def _write_jsonl_file(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def db_connect(*, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{VAULT_DB_PATH}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(VAULT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_database() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS records_status_idx ON records(status);
            CREATE INDEX IF NOT EXISTS records_hash_idx ON records(content_hash);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, history_id TEXT UNIQUE NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT UNIQUE NOT NULL,
                event_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                session_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                source TEXT NOT NULL, observed_at TEXT NOT NULL,
                UNIQUE(candidate_id, session_key, content_hash)
            );
            CREATE INDEX IF NOT EXISTS evidence_candidate_idx ON evidence(candidate_id);
            CREATE TABLE IF NOT EXISTS index_outbox (
                event_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, change_type TEXT NOT NULL,
                queued_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        schema_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if schema_row is not None:
            try:
                stored_schema = int(schema_row["value"])
            except ValueError as exc:
                raise RuntimeError(
                    f"Vault schema_version is not an integer: {schema_row['value']!r}"
                ) from exc
            if stored_schema > CURRENT_SUPPORTED_SCHEMA:
                raise RuntimeError(
                    "Vault schema_version "
                    f"{stored_schema} is newer than supported "
                    f"{CURRENT_SUPPORTED_SCHEMA}; refusing to migrate or overwrite. "
                    "Use a matching Hermes Memory build or restore a known generation."
                )
            if stored_schema < CURRENT_SUPPORTED_SCHEMA:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(CURRENT_SUPPORTED_SCHEMA),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_from_schema_version', ?)",
                    (str(stored_schema),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('migration_marker', ?)",
                    (now_iso(),),
                )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(CURRENT_SUPPORTED_SCHEMA),),
            )
        migration = conn.execute("SELECT value FROM meta WHERE key='legacy_jsonl_migration_complete'").fetchone()
        if migration is None:
            legacy_records = _read_jsonl_file(RECORDS_PATH)
            legacy_history = _read_jsonl_file(HISTORY_PATH)
            legacy_outbox = _read_jsonl_file(OUTBOX_PATH)
            conn.executemany(
                "INSERT OR REPLACE INTO records(id,status,content_hash,updated_at,data) VALUES(?,?,?,?,?)",
                [(r["id"], r.get("status", "active"), r.get("content_hash", ""), r.get("updated_at", ""), json.dumps(r, ensure_ascii=False, sort_keys=True)) for r in legacy_records],
            )
            previous = "0" * 64
            for event in legacy_history:
                canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_hash = hashlib.sha256((previous + canonical).encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO events(history_id,previous_hash,event_hash,event_json) VALUES(?,?,?,?)",
                    (event.get("history_id") or history_id(), previous, event_hash, json.dumps(event, ensure_ascii=False, sort_keys=True)),
                )
                previous = event_hash
            for item in legacy_outbox:
                conn.execute(
                    "INSERT OR IGNORE INTO index_outbox(event_id,record_id,change_type,queued_at) VALUES(?,?,?,?)",
                    (item.get("event_id", ""), item.get("record_id", ""), item.get("change_type", "legacy"), item.get("queued_at", now_iso())),
                )
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('legacy_jsonl_migration_complete','1')")
        conn.commit()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if VAULT_DB_PATH.exists() and path in {RECORDS_PATH, HISTORY_PATH, OUTBOX_PATH}:
        with db_connect(readonly=True) as conn:
            if path == RECORDS_PATH:
                rows = conn.execute("SELECT data FROM records ORDER BY rowid").fetchall()
                return [json.loads(row["data"]) for row in rows]
            if path == HISTORY_PATH:
                rows = conn.execute("SELECT event_json FROM events ORDER BY seq").fetchall()
                return [json.loads(row["event_json"]) for row in rows]
            rows = conn.execute(
                "SELECT event_id,record_id,change_type,queued_at,status,attempts,next_retry_at,last_error FROM index_outbox ORDER BY queued_at,event_id"
            ).fetchall()
            return [dict(row) for row in rows]
    return _read_jsonl_file(path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    _write_jsonl_file(path, records)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


@contextmanager
def vault_lock():
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    depth = int(getattr(_TX_LOCAL, "lock_depth", 0))
    if depth:
        _TX_LOCAL.lock_depth = depth + 1
        try:
            yield
        finally:
            _TX_LOCAL.lock_depth = depth
        return
    with _PROCESS_LOCK:
        with VAULT_LOCK_PATH.open("a+") as lock_fh:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            _TX_LOCAL.lock_depth = 1
            try:
                yield
            finally:
                _TX_LOCAL.lock_depth = 0
                if fcntl is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def locked_mutation(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with vault_lock():
            _TX_LOCAL.events = []
            _TX_LOCAL.evidence = []
            try:
                return func(*args, **kwargs)
            finally:
                _TX_LOCAL.events = []
                _TX_LOCAL.evidence = []
    return wrapper


def save_records(records: list[dict[str, Any]]) -> None:
    init_database()
    validate_record_invariants(records)
    events = list(getattr(_TX_LOCAL, "events", []))
    evidences = list(getattr(_TX_LOCAL, "evidence", []))
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM records")
        conn.executemany(
            "INSERT INTO records(id,status,content_hash,updated_at,data) VALUES(?,?,?,?,?)",
            [(r["id"], r.get("status", "active"), r.get("content_hash", ""), r.get("updated_at", ""), json.dumps(r, ensure_ascii=False, sort_keys=True)) for r in records],
        )
        previous_row = conn.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = previous_row["event_hash"] if previous_row else "0" * 64
        for event in events:
            canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            event_hash = hashlib.sha256((previous + canonical).encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO events(history_id,previous_hash,event_hash,event_json) VALUES(?,?,?,?)",
                (event["history_id"], previous, event_hash, json.dumps(event, ensure_ascii=False, sort_keys=True)),
            )
            previous = event_hash
        for evidence in evidences:
            conn.execute(
                "INSERT OR IGNORE INTO evidence(evidence_id,candidate_id,session_key,content_hash,source,observed_at) VALUES(?,?,?,?,?,?)",
                (evidence["evidence_id"], evidence["candidate_id"], evidence["session_key"], evidence["content_hash"], evidence["source"], evidence["observed_at"]),
            )
        conn.commit()
    _write_jsonl_file(RECORDS_PATH, records)
    _write_jsonl_file(HISTORY_PATH, read_jsonl(HISTORY_PATH))
    _write_jsonl_file(EVIDENCE_PATH, read_evidence_export())
    render_topics(records)
    rebuild_local_index(records)
    write_index(records)


def read_evidence_export() -> list[dict[str, Any]]:
    """Portable emergency export of evidence rows (not a second SoT)."""
    if not VAULT_DB_PATH.exists():
        return _read_jsonl_file(EVIDENCE_PATH)
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT evidence_id,candidate_id,session_key,content_hash,source,observed_at "
            "FROM evidence ORDER BY observed_at,evidence_id"
        ).fetchall()
        return [dict(row) for row in rows]


def record_history(
    change_type: str,
    *,
    record_id: str,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    reason: str = "",
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "history_id": history_id(),
        "id": record_id,
        "changed_at": now_iso(),
        "change_type": change_type,
        "reason": reason,
        "source": source,
    }
    if old is not None:
        entry["old"] = old
    if new is not None:
        entry["new"] = new
    if extra:
        entry.update(extra)
    pending = getattr(_TX_LOCAL, "events", None)
    if pending is None:
        raise RuntimeError("record_history must run inside a vault mutation")
    pending.append(entry)
    return entry


def enqueue_index_event(event: dict[str, Any]) -> None:
    """Compatibility no-op: outbox rows commit atomically with history events."""


def add_evidence(candidate_id: str, *, session_key: str, content_digest: str, source: str) -> dict[str, Any]:
    session_key = session_key.strip() or "unknown-session"
    observed_at = now_iso()
    observation_key = f"{session_key}@{observed_at[:10]}"
    raw = f"{candidate_id}:{observation_key}:{content_digest}"
    evidence = {
        "evidence_id": "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "candidate_id": candidate_id,
        "session_key": observation_key,
        "content_hash": content_digest,
        "source": source,
        "observed_at": observed_at,
    }
    pending = getattr(_TX_LOCAL, "evidence", None)
    if pending is None:
        raise RuntimeError("add_evidence must run inside a vault mutation")
    pending.append(evidence)
    return evidence


def evidence_count(candidate_id: str) -> int:
    init_database()
    with db_connect(readonly=True) as conn:
        row = conn.execute("SELECT COUNT(DISTINCT session_key) AS n FROM evidence WHERE candidate_id=?", (candidate_id,)).fetchone()
    return int(row["n"] if row else 0)


def contains_secret(body: str) -> bool:
    return _governance_manager().contains_secret(body)


def assert_memory_safe(body: str) -> None:
    _governance_manager().assert_memory_safe(body)


def unique_record_id(records: list[dict[str, Any]], namespace: str, key: str) -> str:
    return _governance_manager().unique_record_id(records, namespace, key)


def record_predecessor_id(record: dict[str, Any]) -> str:
    return _governance_manager().record_predecessor_id(record)


def validate_record_invariants(records: list[dict[str, Any]]) -> None:
    _governance_manager().validate_record_invariants(records)


def database_health() -> dict[str, Any]:
    ensure_layout()
    result: dict[str, Any] = {
        "integrity": "unknown", "event_chain_valid": True,
        "record_count": 0, "event_count": 0,
        "outbox_pending": 0, "outbox_retry": 0, "outbox_dead": 0,
    }
    with db_connect(readonly=True) as conn:
        result["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        result["record_count"] = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        rows = conn.execute("SELECT previous_hash,event_hash,event_json FROM events ORDER BY seq").fetchall()
        previous = "0" * 64
        for row in rows:
            canonical = json.dumps(json.loads(row["event_json"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((previous + canonical).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                result["event_chain_valid"] = False
                break
            previous = row["event_hash"]
        result["event_count"] = len(rows)
        for row in conn.execute("SELECT status,COUNT(*) AS n FROM index_outbox GROUP BY status").fetchall():
            key = f"outbox_{row['status']}"
            if key in result:
                result[key] = int(row["n"])
    result["healthy"] = result["integrity"] == "ok" and result["event_chain_valid"] and result["outbox_dead"] == 0
    return result


def active_records_fingerprint(records: list[dict[str, Any]]) -> str:
    return _local_index_manager().active_records_fingerprint(records)


def search_terms(text: str) -> list[str]:
    return _local_index_manager().search_terms(text)


def local_embedding_text(record: dict[str, Any]) -> str:
    return _local_index_manager().embedding_text(record)


def get_local_embedder():
    global _LOCAL_EMBEDDER
    with _EMBEDDER_LOCK:
        if _LOCAL_EMBEDDER is None:
            # Prefer an already-installed fastembed. lazy_deps.ensure is only a
            # recovery path for a rebuilt venv — it must not gate a working
            # install (memory.vault is not always in LAZY_DEPS).
            try:
                from fastembed import TextEmbedding
            except Exception:
                try:
                    from tools.lazy_deps import ensure
                    ensure("memory.vault", prompt=False)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error(
                        "Vault vector dependency (fastembed) unavailable: %s. "
                        "Semantic/vector retrieval will be degraded to lexical-only.",
                        exc,
                    )
                    raise
                from fastembed import TextEmbedding

            _LOCAL_EMBEDDER = TextEmbedding(
                model_name=LOCAL_EMBEDDING_MODEL,
                cache_dir=str(LOCAL_EMBEDDING_CACHE),
                threads=1,
            )
    return _LOCAL_EMBEDDER


def _governance_manager() -> MemoryGovernance:
    return MemoryGovernance(
        valid_statuses=VALID_STATUSES,
        id_factory=lambda namespace, key: stable_id(namespace, key),
        title_factory=lambda body: title_for(body),
        content_hash_factory=lambda body: content_hash(body),
    )


def _local_index_manager() -> LocalSearchIndex:
    return LocalSearchIndex(
        path=LOCAL_INDEX_PATH,
        schema_version=LOCAL_INDEX_SCHEMA_VERSION,
        embedding_model=LOCAL_EMBEDDING_MODEL,
        alias_groups=SEARCH_ALIAS_GROUPS,
        embedder_factory=lambda: get_local_embedder(),
        ensure_layout=ensure_layout,
        lock_factory=vault_lock,
        embedding_revision=LOCAL_EMBEDDING_REVISION,
        distance_metric="cosine_dot_normalized",
    )


def _normalized_vector(values: Any):
    return _local_index_manager().normalized_vector(values)


def _build_local_index(
    records: list[dict[str, Any]],
    target: Path,
) -> None:
    _local_index_manager().build(records, target)


def rebuild_local_index(records: list[dict[str, Any]]) -> None:
    _local_index_manager().rebuild(records)


def ensure_local_index(records: list[dict[str, Any]]) -> bool:
    return _local_index_manager().ensure(records)


def local_index_health(
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = records if records is not None else read_jsonl(RECORDS_PATH)
    health = _local_index_manager().health(current)
    runtime_available = importlib.util.find_spec("fastembed") is not None
    health["embedding_runtime_available"] = runtime_available
    if health.get("active_records") and not runtime_available:
        health["semantic_ready"] = False
        health["mode"] = "lexical-only"
        # Log explicit ERROR so silent degradation is surfaced.
        logger.error(
            "Vault vector backend unavailable: dependency=fastembed "
            "component=vault/local-vector status=missing "
            "semantic/vector retrieval degraded to lexical-only"
        )
    return health


def local_search_mode() -> str:
    return str(local_index_health().get("mode", "lexical-only"))


def _fts_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    return _local_index_manager().fts_search(conn, query, top_k=top_k)


def _vector_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    return _local_index_manager().vector_search(conn, query, top_k=top_k)


def local_search(query: str, *, top_k: int = 10) -> list[dict[str, Any]]:
    """Search the authoritative local catalog with FTS5 and local embeddings."""
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    return _local_index_manager().search(records, query, top_k=top_k)


def active_records_by_id() -> dict[str, dict[str, Any]]:
    ensure_layout()
    return {r["id"]: r for r in read_jsonl(RECORDS_PATH) if r.get("status") == "active"}


def resolve_active_successor(
    records: list[dict[str, Any]],
    record_id: str,
) -> str:
    """Follow a supersession chain to its single current active version."""
    by_id = {str(record.get("id", "")): record for record in records}
    current_id = str(record_id or "")
    seen: set[str] = set()
    while current_id:
        if current_id in seen:
            raise SystemExit(
                f"Supersession cycle detected while resolving {record_id}"
            )
        seen.add(current_id)
        current = by_id.get(current_id)
        if current is None:
            raise SystemExit(f"No record found for id {current_id}")
        if current.get("status") == "active":
            return current_id
        if current.get("status") != "superseded":
            raise SystemExit(f"Record {current_id} is not active")
        successor_id = str(current.get("superseded_by", ""))
        if not successor_id:
            raise SystemExit(
                f"Superseded record {current_id} has no successor"
            )
        current_id = successor_id
    raise SystemExit("Merge requires an active memory id")


def pending_records() -> list[dict[str, Any]]:
    ensure_layout()
    active = active_records_by_id()
    rows: list[dict[str, Any]] = []
    for record in read_jsonl(RECORDS_PATH):
        if record.get("status") != "pending":
            continue
        matched = active.get(str(record.get("matched_id", "")), {})
        rows.append({
            "id": record["id"],
            "title": record.get("title", ""),
            "summary": record.get("summary", ""),
            "topic": record.get("topic", "other"),
            "risk": record.get("risk", ""),
            "review_status": record.get("review_status", ""),
            "governance_action": record.get("governance_action", ""),
            "matched_id": record.get("matched_id", ""),
            "matched_summary": matched.get("summary", ""),
            "evidence_count": evidence_count(record["id"]),
            "created_at": record.get("created_at", ""),
        })
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


@locked_mutation
def reclassify_pending_risk(
    record_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    records = read_jsonl(RECORDS_PATH)
    record = next(
        (
            item
            for item in records
            if item.get("id") == record_id
            and item.get("status") == "pending"
        ),
        None,
    )
    if record is None:
        raise SystemExit(f"No pending record found for id {record_id}")
    if not is_high_risk_memory(
        str(record.get("body", "")),
        str(record.get("topic", "other")),
    ):
        raise SystemExit(
            f"Current policy does not classify {record_id} as high risk"
        )
    if (
        record.get("risk") == "high"
        and record.get("review_status") == "needs_user_review"
    ):
        return record
    old = dict(record)
    record["risk"] = "high"
    record["review_status"] = "needs_user_review"
    record["decision_reason"] = reason
    record["updated_at"] = now_iso()
    record_history(
        "candidate_risk_reclassified",
        record_id=record_id,
        old=old,
        new=dict(record),
        reason=reason,
        source="memory_governor.py",
    )
    save_records(records)
    return record


def review_record(candidate_id: str, decision: str, *, active_id: str = "", reason: str = "user review") -> dict[str, Any]:
    decision = decision.strip().lower()
    if decision == "approve":
        pending = {row["id"]: row for row in pending_records()}
        matched_id = str(pending.get(candidate_id, {}).get("matched_id", ""))
        if matched_id:
            return merge_record(candidate_id, matched_id, reason=reason)
        return promote_record(candidate_id, reason=reason)
    if decision == "reject":
        return reject_record(candidate_id, reason=reason)
    if decision == "merge":
        if not active_id:
            pending = {row["id"]: row for row in pending_records()}
            active_id = str(pending.get(candidate_id, {}).get("matched_id", ""))
        if not active_id:
            raise SystemExit("Merge requires an active memory id")
        return merge_record(candidate_id, active_id, reason=reason)
    raise SystemExit("decision must be approve, reject, or merge")


@locked_mutation
def import_pending_record(record: dict[str, Any], *, reason: str, source: str) -> tuple[dict[str, Any], bool]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    legacy_id = str(record.get("legacy_mem0_id", ""))
    for existing in records:
        if existing.get("id") == record.get("id") or (legacy_id and existing.get("legacy_mem0_id") == legacy_id):
            return existing, False
    records.append(record)
    record_history(
        "legacy_mem0_import", record_id=record["id"], new=dict(record),
        reason=reason, source=source,
    )
    save_records(records)
    return record, True


def body_similarity(left: str, right: str) -> float:
    return _governance_manager().body_similarity(left, right)


def nearest_active(records: list[dict[str, Any]], body: str, *, topic: str = "") -> dict[str, Any] | None:
    return _governance_manager().nearest_active(
        records,
        body,
        topic=topic,
    )


def is_high_risk_memory(body: str, topic: str) -> bool:
    return _governance_manager().is_high_risk_memory(body, topic)


def governance_decision(body: str, records: list[dict[str, Any]], *, topic: str) -> dict[str, Any]:
    return _governance_manager().decision(body, records, topic=topic)


def read_core_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    return [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]


def title_for(entry: str) -> str:
    first = normalize(entry.splitlines()[0] if entry else "")
    first = first.lstrip("#").strip()
    if "：" in first:
        first = first.split("：", 1)[0].strip()
    if ":" in first and len(first.split(":", 1)[0]) < 40:
        first = first.split(":", 1)[0].strip()
    return first[:80] or "untitled memory"


def classify(entry: str, source_file: str) -> str:
    text = entry.lower()
    if source_file == "USER.md":
        return "profile"
    if any(k in entry for k in ("八字", "命理", "用神", "太岁", "道教", "排盘")):
        return "astrology"
    if any(k in entry for k in ("高血压", "体检", "替尔泊肽", "护肤", "洗护", "补剂", "用药", "BMI", "脂肪肝", "步态", "瑞慈")):
        return "health"
    if any(k in entry for k in ("宝宝", "封静", "月嫂", "纸尿裤", "婴儿", "满月", "瑞杉", "家用选品")):
        return "family"
    if any(k in entry for k in ("叁一源", "财税", "社保", "增值税", "所得税", "股权", "黄金", "合伙人", "人才项目")):
        return "company_finance"
    if any(k in entry for k in ("Codex", "MCP", "Home Assistant", "MiMo", "Telegram", "TTS", "Git Proxy", "auth.json", "cron", "HA")):
        return "hermes_ops"
    if any(k in entry for k in ("修复验证", "工作流", "科学分析", "营销话术", "提醒", "grep验证")):
        return "workflow"
    if any(k in entry for k in ("音频", "Apple Music", "真力", "FitEar", "Meze", "DSD", "耳机")):
        return "audio"
    if any(k in entry for k in ("旅游", "瑞士", "新西兰", "皇后镇", "劳特布伦嫩")):
        return "travel"
    if any(k in entry for k in ("游戏", "盛世天下", "Windows PC", "Model Y")):
        return "gaming"
    if any(k in entry for k in ("选购", "采购", "产品", "品牌", "小米", "Apple", "超声波", "衣服", "尺码", "手链", "手串", "舒乐氏")):
        return "products"
    return "other"


def core_policy(topic: str, body: str) -> str:
    if topic in CORE_KEEP_TOPICS:
        return "keep"
    if len(body) <= 80 and topic in {"family", "health", "company_finance"}:
        return "keep_summary"
    return "archive_candidate"


def make_record(entry: str, source_file: str, index: int) -> dict[str, Any]:
    topic = classify(entry, source_file)
    title = title_for(entry)
    source = f"core:{source_file}:{index}"
    ts = now_iso()
    return {
        "id": stable_id(source_file, title),
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "topic": topic,
        "title": title,
        "summary": normalize(entry)[:240],
        "body": entry,
        "tags": [topic, source_file.replace(".md", "").lower()],
        "source": {
            "kind": "core",
            "file": source_file,
            "entry_index": index,
        },
        "content_hash": content_hash(entry),
        "created_at": ts,
        "updated_at": ts,
        "core_policy": core_policy(topic, entry),
    }


def ensure_layout() -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not RECORDS_PATH.exists():
        _write_jsonl_file(RECORDS_PATH, [])
    if not HISTORY_PATH.exists():
        _write_jsonl_file(HISTORY_PATH, [])
    init_database()
    if not README_PATH.exists():
        README_PATH.write_text(
            "# Hermes Memory Vault\n\n"
            "Local authority store for long-term memories. MEMORY.md and USER.md stay small; "
            "this vault stores durable facts, history, and topic views.\n\n"
            "Files:\n"
            "- `vault.sqlite3`: transactional authoritative records, events, and evidence.\n"
            "- `memories.jsonl`: portable export of active/archived records.\n"
            "- `history.jsonl`: portable export of immutable history events.\n"
            "- `topics/*.md`: generated topic views for human browsing.\n"
            "- `local-search.sqlite3`: local FTS5/embedding retrieval index.\n\n"
            "Status values: `pending`, `active`, `superseded`, `archived`, `disputed`, `rejected`.\n",
            encoding="utf-8",
        )


def write_index(records: list[dict[str, Any]]) -> None:
    active = [r for r in records if r.get("status") == "active"]
    by_topic: dict[str, int] = {}
    by_policy: dict[str, int] = {}
    for r in active:
        by_topic[r.get("topic", "other")] = by_topic.get(r.get("topic", "other"), 0) + 1
        by_policy[r.get("core_policy", "unknown")] = by_policy.get(r.get("core_policy", "unknown"), 0) + 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "record_count": len(records),
        "active_count": len(active),
        "topics": by_topic,
        "core_policy": by_policy,
        "authority": str(VAULT_DB_PATH),
        "history": str(HISTORY_PATH),
    }
    INDEX_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_topics(records: list[dict[str, Any]]) -> None:
    active = [r for r in records if r.get("status") == "active"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in active:
        grouped.setdefault(record.get("topic", "other"), []).append(record)

    for existing in TOPICS_DIR.glob("*.md"):
        existing.unlink()

    for topic, rows in sorted(grouped.items()):
        rows.sort(key=lambda r: (r.get("title", ""), r.get("id", "")))
        lines = [
            f"# {topic}",
            "",
            TOPIC_LABELS.get(topic, ""),
            "",
            f"Records: {len(rows)}",
            "",
        ]
        for record in rows:
            lines.append(f"## {record['title']}")
            lines.append("")
            lines.append(f"- id: `{record['id']}`")
            lines.append(f"- status: `{record.get('status', 'active')}`")
            lines.append(f"- core_policy: `{record.get('core_policy', 'unknown')}`")
            lines.append(f"- updated_at: `{record.get('updated_at', '')}`")
            lines.append("")
            lines.append(record.get("body", ""))
            lines.append("")
        (TOPICS_DIR / f"{slugify(topic)}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_mem0_seed(records: list[dict[str, Any]]) -> None:
    rows = []
    for record in records:
        if record.get("status") != "active":
            continue
        rows.append({
            "memory_id": record["id"],
            "topic": record.get("topic", "other"),
            "text": f"[{record['id']}] {record.get('title', '')}: {record.get('summary', '')}",
            "source": "memory-vault",
            "updated_at": record.get("updated_at"),
        })
    write_jsonl(MEM0_SEED_PATH, rows)


def mem0_keywords(record: dict[str, Any]) -> str:
    text = " ".join([
        str(record.get("title", "")),
        str(record.get("topic", "")),
        " ".join(str(tag) for tag in record.get("tags", [])),
        str(record.get("body", "")),
    ])
    code_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_./~-]{2,}", text)
    aliases: list[str] = []
    if any(marker in text for marker in ("Home Assistant", "ha_control", "ha_set_temp", "ha_token")):
        aliases.extend([
            "Home Assistant", "HA", "air conditioner", "aircon", "AC",
            "ha_control.py", "ha_set_temp.py", "ha_token", "token", "空调", "控制",
        ])
    if any(marker in text for marker in ("Codex", "Hermes", "MCP", "cron", "TTS", "Telegram")):
        aliases.extend(["Hermes", "Codex", "MCP", "automation", "agent", "cron", "gateway"])
    if any(marker in text for marker in ("护肤", "高血压", "替尔泊肽", "补剂", "BMI")):
        aliases.extend(["health", "skincare", "supplement", "medication", "护肤", "健康"])
    tokens = []
    seen = set()
    for token in [*code_tokens, *aliases]:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
        if len(tokens) >= 80:
            break
    return " ".join(tokens)


def mem0_text(record: dict[str, Any]) -> str:
    keywords = mem0_keywords(record)
    keyword_line = f"Keywords: {keywords}\n" if keywords else ""
    return (
        f"[memory-vault:{record['id']}] {record.get('title', '')}\n"
        f"Topic: {record.get('topic', 'other')}\n"
        f"Updated: {record.get('updated_at', '')}\n\n"
        f"{keyword_line}"
        f"{record.get('body', '')}"
    ).strip()


def parse_tool_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except Exception:
        return {"error": raw}
    return payload if isinstance(payload, dict) else {"result": payload}


def is_qdrant_lock_error(message: str) -> bool:
    lowered = str(message or "").lower()
    return "already accessed by another instance of qdrant client" in lowered


def group_mem0_vault_items(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_record_id: dict[str, list[dict[str, Any]]] = {}
    marker_re = re.compile(r"memory-vault:(mem_[0-9a-f]+)")
    for item in items:
        memory = str(item.get("memory", ""))
        match = marker_re.search(memory)
        if match:
            by_record_id.setdefault(match.group(1), []).append(item)
    return by_record_id


def list_mem0_vault_items(provider, *, top_k: int) -> tuple[dict[str, list[dict[str, Any]]], list[Any]]:
    errors: list[Any] = []
    try:
        backend = getattr(provider, "_backend", None)
        memory = getattr(backend, "_memory", None)
        if memory is not None:
            response = memory.get_all(filters=provider._read_filters(), top_k=top_k)
            results = response.get("results", []) if isinstance(response, dict) else response
            if isinstance(results, list):
                return group_mem0_vault_items(results), errors
    except Exception as exc:
        errors.append({"stage": "list_backend", "error": f"{type(exc).__name__}: {exc}"})
        return {}, errors

    by_record_id: dict[str, list[dict[str, Any]]] = {}
    page = 1
    page_size = 200
    while True:
        payload = parse_tool_json(provider.handle_tool_call(
            "mem0_list",
            {"page": page, "page_size": page_size},
        ))
        if payload.get("error"):
            errors.append({"stage": "list", "page": page, "error": payload["error"]})
            break
        results = payload.get("results", [])
        if not isinstance(results, list):
            break
        for record_id, items in group_mem0_vault_items(results).items():
            by_record_id.setdefault(record_id, []).extend(items)
        count = int(payload.get("count", len(results)) or 0)
        if not results or page * page_size >= count:
            break
        page += 1
    return by_record_id, errors


def load_mem0_provider():
    os.environ.setdefault("POSTHOG_DISABLED", "true")
    os.environ.setdefault("MEM0_TELEMETRY", "false")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    if str(HERMES_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT_DIR))
    try:
        import posthog

        posthog.disabled = True
    except Exception:
        pass
    module = importlib.import_module("plugins.memory.mem0")
    provider = module.Mem0MemoryProvider()
    provider.initialize("memory-vault-sync", platform="memory-vault", user_id="max")
    return provider


def sync_mem0(*, dry_run: bool = False, limit: int = 0) -> dict[str, Any]:
    ensure_layout()
    all_active_records = [
        record for record in read_jsonl(RECORDS_PATH)
        if record.get("status") == "active"
    ]
    records = all_active_records
    if limit:
        records = records[:limit]
    write_mem0_seed(read_jsonl(RECORDS_PATH))

    readiness = mem0_readiness()
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "partial": bool(limit),
        "records_considered": len(records),
        "active_records": len(all_active_records),
        "ready_for_shadow": readiness["ready_for_shadow"],
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "deleted_duplicates": 0,
        "deleted_stale": 0,
        "errors": [],
    }
    if dry_run:
        summary["would_sync"] = len(records)
        return summary
    if not readiness["ready_for_shadow"]:
        summary["errors"].append("mem0 is not ready for shadow indexing; run audit for missing dependencies/models.")
        return summary

    provider = load_mem0_provider()
    try:
        init_error = getattr(provider, "_init_error", "")
        if getattr(provider, "_backend", None) is None and is_qdrant_lock_error(init_error):
            summary["deferred"] = True
            summary["defer_reason"] = (
                "local Qdrant path is locked by another Hermes process; "
                "sync will retry on the next audit or after moving mem0 to Qdrant server mode"
            )
            return summary
        existing_by_id, list_errors = list_mem0_vault_items(
            provider,
            top_k=max(200, len(records) * 3),
        )
        summary["errors"].extend(list_errors)
        if list_errors:
            return summary
        active_ids = {record["id"] for record in all_active_records}
        for record in records:
            text = mem0_text(record)
            matches = existing_by_id.get(record["id"], [])
            if matches:
                current = str(matches[0].get("memory", ""))
                if current.strip() == text.strip():
                    summary["unchanged"] += 1
                else:
                    update = parse_tool_json(provider.handle_tool_call(
                        "mem0_update",
                        {"memory_id": matches[0].get("id", ""), "text": text},
                    ))
                    if update.get("error"):
                        summary["errors"].append({"id": record["id"], "stage": "update", "error": update["error"]})
                        continue
                    summary["updated"] += 1
                for duplicate in matches[1:]:
                    delete = parse_tool_json(provider.handle_tool_call(
                        "mem0_delete",
                        {"memory_id": duplicate.get("id", "")},
                    ))
                    if delete.get("error"):
                        summary["errors"].append({
                            "id": record["id"],
                            "stage": "delete_duplicate",
                            "error": delete["error"],
                        })
                        continue
                    summary["deleted_duplicates"] += 1
                continue
            add = parse_tool_json(provider.handle_tool_call("mem0_add", {"content": text}))
            if add.get("error"):
                summary["errors"].append({"id": record["id"], "stage": "add", "error": add["error"]})
                continue
            summary["added"] += 1
        for stale_record_id, stale_matches in existing_by_id.items():
            if stale_record_id in active_ids:
                continue
            for stale in stale_matches:
                delete = parse_tool_json(provider.handle_tool_call(
                    "mem0_delete",
                    {"memory_id": stale.get("id", "")},
                ))
                if delete.get("error"):
                    summary["errors"].append({
                        "id": stale_record_id,
                        "stage": "delete_stale",
                        "error": delete["error"],
                    })
                    continue
                summary["deleted_stale"] += 1
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
    return summary


def pending_index_events() -> list[dict[str, Any]]:
    """Return index events eligible for replay without treating the index as authority."""
    ensure_layout()
    with db_connect(readonly=True) as conn:
        rows = conn.execute(
            """SELECT event_id,record_id,change_type,queued_at,status,attempts,next_retry_at,last_error
               FROM index_outbox
               WHERE status IN ('pending','retry') AND (next_retry_at='' OR next_retry_at<=?)
               ORDER BY queued_at,event_id""",
            (now_iso(),),
        ).fetchall()
    return [dict(row) for row in rows]


def sync_index_outbox(*, dry_run: bool = False, reconcile: bool = True) -> dict[str, Any]:
    """Synchronize all outstanding vault mutations to mem0/Qdrant atomically enough for replay.

    The operation deliberately performs an idempotent full active-record reconciliation.
    This keeps failure recovery simple: an interrupted run leaves the outbox intact and
    the next run can safely replay it.
    """
    pending = pending_index_events()
    result: dict[str, Any] = {
        "pending_events": len(pending),
        "acknowledged": 0,
        "sync": None,
        "errors": [],
        "degraded": False,
        "reconciled": False,
    }
    if not pending and not reconcile:
        return result
    if dry_run:
        result["would_sync"] = len(pending)
        result["would_reconcile"] = reconcile
        return result
    summary = sync_mem0()
    result["sync"] = summary
    result["reconciled"] = True
    if summary.get("errors") or summary.get("deferred"):
        result["errors"] = list(summary.get("errors", []))
        if summary.get("deferred"):
            result["errors"].append(str(summary.get("defer_reason", "index sync deferred")))
        result["degraded"] = True
        detail = json.dumps(result["errors"], ensure_ascii=False, sort_keys=True)[:2000]
        with db_connect() as conn:
            for event in pending:
                attempts = int(event.get("attempts", 0)) + 1
                delay_minutes = min(360, 2 ** min(attempts, 8))
                retry_at = (dt.datetime.now().astimezone() + dt.timedelta(minutes=delay_minutes)).isoformat(timespec="seconds")
                status = "dead" if attempts >= 10 else "retry"
                conn.execute(
                    "UPDATE index_outbox SET status=?,attempts=?,next_retry_at=?,last_error=? WHERE event_id=?",
                    (status, attempts, retry_at, detail, event["event_id"]),
                )
            conn.commit()
        _write_jsonl_file(OUTBOX_PATH, read_jsonl(OUTBOX_PATH))
        return result
    with db_connect() as conn:
        conn.executemany(
            "UPDATE index_outbox SET status='acked',last_error='',next_retry_at='' WHERE event_id=?",
            [(event["event_id"],) for event in pending],
        )
        conn.commit()
    _write_jsonl_file(OUTBOX_PATH, read_jsonl(OUTBOX_PATH))
    result["acknowledged"] = len(pending)
    return result


@locked_mutation
def import_core() -> tuple[int, int]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    by_id = {r["id"]: r for r in records}
    added = 0
    updated = 0

    for source_file in ("MEMORY.md", "USER.md"):
        for idx, entry in enumerate(read_core_entries(MEMORIES_DIR / source_file), 1):
            record = make_record(entry, source_file, idx)
            existing = by_id.get(record["id"])
            if existing is None:
                records.append(record)
                by_id[record["id"]] = record
                added += 1
                continue
            if existing.get("content_hash") != record["content_hash"]:
                old = dict(existing)
                existing.update({
                    "topic": record["topic"],
                    "title": record["title"],
                    "summary": record["summary"],
                    "body": record["body"],
                    "tags": record["tags"],
                    "source": record["source"],
                    "content_hash": record["content_hash"],
                    "updated_at": now_iso(),
                    "core_policy": record["core_policy"],
                })
                record_history(
                    "core_refresh",
                    record_id=existing["id"],
                    old=old,
                    new=dict(existing),
                    reason="core memory import refreshed changed content",
                    source=f"core:{source_file}:{idx}",
                )
                updated += 1

    save_records(records)
    return added, updated


@locked_mutation
def add_record(
    body: str,
    *,
    title: str | None = None,
    topic: str | None = None,
    tags: list[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    ensure_layout()
    body = body.strip()
    if not body:
        raise SystemExit("Memory body cannot be empty")
    assert_memory_safe(body)

    records = read_jsonl(RECORDS_PATH)
    digest = content_hash(body)
    for record in records:
        if record.get("status") == "active" and record.get("content_hash") == digest:
            return record, False

    resolved_topic = topic or classify(body, "")
    resolved_title = title or title_for(body)
    tag_values: list[str] = [resolved_topic, "manual"]
    for tag in tags or []:
        for part in tag.split(","):
            value = part.strip()
            if value and value not in tag_values:
                tag_values.append(value)

    ts = now_iso()
    record = {
        "id": unique_record_id(
            records, "manual", f"{resolved_title}:{digest[:16]}"
        ),
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "topic": resolved_topic,
        "title": resolved_title,
        "summary": normalize(body)[:240],
        "body": body,
        "tags": tag_values,
        "source": {
            "kind": "manual",
        },
        "content_hash": digest,
        "created_at": ts,
        "updated_at": ts,
        "core_policy": core_policy(resolved_topic, body),
    }
    records.append(record)
    record_history(
        "manual_add",
        record_id=record["id"],
        new=dict(record),
        reason="manual add command",
        source="memory_vault.py add",
    )
    save_records(records)
    return record, True


@locked_mutation
def propose_record(
    body: str,
    *,
    title: str | None = None,
    topic: str | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
    evidence_session: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    ensure_layout()
    body = body.strip()
    if not body:
        raise SystemExit("Memory body cannot be empty")
    assert_memory_safe(body)

    records = read_jsonl(RECORDS_PATH)
    digest = content_hash(body)
    for record in records:
        if record.get("status") in {"pending", "active"} and record.get("content_hash") == digest:
            if record.get("status") == "pending":
                if (
                    record.get("risk") != "high"
                    and is_high_risk_memory(
                        body,
                        str(record.get("topic", "other")),
                    )
                ):
                    old = dict(record)
                    record["risk"] = "high"
                    record["review_status"] = "needs_user_review"
                    record["decision_reason"] = (
                        "reclassified by current sensitive-memory policy"
                    )
                    record["updated_at"] = now_iso()
                    record_history(
                        "candidate_risk_reclassified",
                        record_id=record["id"],
                        old=old,
                        new=dict(record),
                        reason=record["decision_reason"],
                        source=source,
                    )
                add_evidence(
                    record["id"], session_key=evidence_session or source,
                    content_digest=digest, source=source,
                )
                save_records(records)
            return record, False

    resolved_topic = topic or classify(body, "")
    resolved_title = title or title_for(body)
    decision = governance_decision(body, records, topic=resolved_topic)
    tag_values: list[str] = [resolved_topic, "candidate"]
    for tag in tags or []:
        for part in tag.split(","):
            value = part.strip()
            if value and value not in tag_values:
                tag_values.append(value)

    ts = now_iso()
    candidate_metadata = {
        str(key): value
        for key, value in dict(metadata or {}).items()
        if str(key) in {
            "domain",
            "source_session_id",
            "source_trace_id",
            "source_type",
            "confidence",
            "sensitivity",
            "recall_scope",
            "legacy_fact_id",
        }
    }
    record = {
        "id": unique_record_id(
            records, "pending", f"{resolved_title}:{digest[:16]}"
        ),
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "review_status": decision["review_status"],
        "governance_action": decision["action"],
        "risk": decision["risk"],
        "matched_id": decision.get("matched_id", ""),
        "confidence": decision.get("confidence", 0.0),
        "decision_reason": decision["reason"],
        "topic": resolved_topic,
        "title": resolved_title,
        "summary": normalize(body)[:240],
        "body": body,
        "tags": tag_values,
        "source": {
            "kind": "candidate",
            "origin": source,
            "metadata": candidate_metadata,
        },
        "content_hash": digest,
        "created_at": ts,
        "updated_at": ts,
        "core_policy": core_policy(resolved_topic, body),
    }
    records.append(record)
    add_evidence(
        record["id"], session_key=evidence_session or source,
        content_digest=digest, source=source,
    )
    record_history(
        "candidate_proposed",
        record_id=record["id"],
        new=dict(record),
        reason=decision["reason"],
        source=source,
    )
    save_records(records)
    return record, True


@locked_mutation
def propose_update(
    active_id: str,
    body: str,
    *,
    source: str,
    evidence_session: str,
) -> tuple[dict[str, Any], bool]:
    ensure_layout()
    body = body.strip()
    if not body:
        raise SystemExit("Memory body cannot be empty")
    assert_memory_safe(body)
    records = read_jsonl(RECORDS_PATH)
    active = next((record for record in records if record.get("id") == active_id and record.get("status") == "active"), None)
    if active is None:
        raise SystemExit(f"No active record found for id {active_id}")
    digest = content_hash(body)
    if active.get("content_hash") == digest:
        return active, False
    for record in records:
        if record.get("status") == "pending" and record.get("content_hash") == digest and record.get("matched_id") == active_id:
            add_evidence(record["id"], session_key=evidence_session, content_digest=digest, source=source)
            save_records(records)
            return record, False
    topic = str(active.get("topic", "other"))
    high_risk = is_high_risk_memory(body, topic)
    ts = now_iso()
    record = {
        "id": unique_record_id(
            records, "pending-update", f"{active_id}:{digest[:16]}"
        ),
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "review_status": "needs_user_review" if high_risk else "pending",
        "governance_action": "merge",
        "risk": "high" if high_risk else "medium",
        "matched_id": active_id,
        "confidence": 1.0,
        "decision_reason": f"explicit correction candidate for active memory {active_id}",
        "topic": topic,
        "title": title_for(body),
        "summary": normalize(body)[:240],
        "body": body,
        "tags": [topic, "candidate", "update"],
        "source": {"kind": "candidate_update", "origin": source, "supersedes": active_id},
        "content_hash": digest,
        "created_at": ts,
        "updated_at": ts,
        "core_policy": core_policy(topic, body),
    }
    records.append(record)
    add_evidence(record["id"], session_key=evidence_session, content_digest=digest, source=source)
    record_history(
        "candidate_update_proposed", record_id=record["id"], new=dict(record),
        reason=record["decision_reason"], source=source,
    )
    save_records(records)
    return record, True


@locked_mutation
def promote_record(record_id: str, *, reason: str = "promote candidate") -> dict[str, Any]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    for record in records:
        if record.get("id") != record_id:
            continue
        if record.get("status") != "pending":
            raise SystemExit(f"Record {record_id} is not pending")
        if (
            record.get("governance_action") == "merge"
            and record.get("matched_id")
        ):
            raise SystemExit(
                f"Record {record_id} replaces {record['matched_id']}; use merge instead of promote"
            )
        old = dict(record)
        record.update({
            "status": "active",
            "review_status": "promoted",
            "governance_action": "add",
            "updated_at": now_iso(),
        })
        record_history(
            "candidate_promoted",
            record_id=record_id,
            old=old,
            new=dict(record),
            reason=reason,
            source="memory_vault.py promote",
        )
        save_records(records)
        return record
    raise SystemExit(f"No record found for id {record_id}")


@locked_mutation
def reject_record(record_id: str, *, reason: str = "reject candidate") -> dict[str, Any]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    for record in records:
        if record.get("id") != record_id:
            continue
        if record.get("status") != "pending":
            raise SystemExit(f"Record {record_id} is not pending")
        old = dict(record)
        record.update({
            "status": "rejected",
            "review_status": "rejected",
            "updated_at": now_iso(),
            "reject_reason": reason,
        })
        record_history(
            "candidate_rejected",
            record_id=record_id,
            old=old,
            new=dict(record),
            reason=reason,
            source="memory_vault.py reject",
        )
        save_records(records)
        return record
    raise SystemExit(f"No record found for id {record_id}")


@locked_mutation
def merge_record(candidate_id: str, active_id: str, *, reason: str = "merge candidate into active memory") -> dict[str, Any]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    candidate = next((r for r in records if r.get("id") == candidate_id), None)
    requested_active_id = active_id
    active = next((r for r in records if r.get("id") == active_id), None)
    if candidate is None:
        raise SystemExit(f"No candidate found for id {candidate_id}")
    if active is None:
        raise SystemExit(f"No active record found for id {active_id}")
    if candidate.get("status") != "pending":
        raise SystemExit(f"Record {candidate_id} is not pending")
    if active.get("status") != "active":
        active_id = resolve_active_successor(records, active_id)
        active = next(r for r in records if r.get("id") == active_id)

    old_candidate = dict(candidate)
    old_active = dict(active)
    ts = now_iso()
    active.update({
        "status": "superseded",
        "updated_at": ts,
        "superseded_by": candidate_id,
    })
    merged_source = {
        **(candidate.get("source") if isinstance(candidate.get("source"), dict) else {}),
        "kind": "merge",
        "supersedes": active_id,
    }
    if requested_active_id != active_id:
        merged_source["requested_supersedes"] = requested_active_id
    candidate.update({
        "status": "active",
        "review_status": "merged",
        "governance_action": "merge",
        "matched_id": active_id,
        "updated_at": ts,
        "source": merged_source,
    })
    history_extra = {
        "records_before": [old_active, old_candidate],
        "records_after": [dict(active), dict(candidate)],
    }
    if requested_active_id != active_id:
        history_extra.update({
            "requested_active_id": requested_active_id,
            "resolved_active_id": active_id,
        })
    record_history(
        "candidate_merged",
        record_id=candidate_id,
        reason=reason,
        source="memory_vault.py merge",
        extra=history_extra,
    )
    save_records(records)
    return candidate


@locked_mutation
def rollback_history(target_history_id: str, *, reason: str = "rollback") -> dict[str, Any]:
    ensure_layout()
    history = read_jsonl(HISTORY_PATH)
    entry = next((h for h in history if h.get("history_id") == target_history_id), None)
    if entry is None:
        raise SystemExit(f"No history entry found for id {target_history_id}")

    records = read_jsonl(RECORDS_PATH)
    by_id = {record.get("id"): record for record in records}
    before_records = entry.get("records_before")
    restored: list[dict[str, Any]] = []
    if isinstance(before_records, list):
        for old in before_records:
            if not isinstance(old, dict) or not old.get("id"):
                continue
            current = by_id.get(old["id"])
            old = dict(old)
            old["updated_at"] = now_iso()
            if current is None:
                records.append(old)
                by_id[old["id"]] = old
            else:
                current.clear()
                current.update(old)
            restored.append(old)
    elif isinstance(entry.get("old"), dict):
        old = dict(entry["old"])
        old["updated_at"] = now_iso()
        current = by_id.get(old.get("id"))
        if current is None:
            records.append(old)
        else:
            current.clear()
            current.update(old)
        restored.append(old)
    else:
        raise SystemExit(f"History entry {target_history_id} has no rollback payload")

    rollback = record_history(
        "rollback",
        record_id=entry.get("id", target_history_id),
        reason=reason,
        source="memory_vault.py rollback",
        extra={
            "rolled_back_history_id": target_history_id,
            "records_restored": restored,
        },
    )
    save_records(records)
    return rollback


@locked_mutation
def update_record(record_id: str, body: str, *, title: str | None = None, topic: str | None = None) -> None:
    ensure_layout()
    body = body.strip()
    if not body:
        raise SystemExit("Memory body cannot be empty")
    assert_memory_safe(body)
    records = read_jsonl(RECORDS_PATH)
    for record in records:
        if record.get("id") != record_id:
            continue
        if record.get("status") != "active":
            raise SystemExit(
                f"Record {record_id} is {record.get('status')}, not active; "
                "use review/merge/rollback for version transitions"
            )
        old = dict(record)
        new_topic = topic or record.get("topic", "other")
        record.update({
            "topic": new_topic,
            "title": title or title_for(body),
            "summary": normalize(body)[:240],
            "body": body,
            "content_hash": content_hash(body),
            "updated_at": now_iso(),
            "core_policy": core_policy(new_topic, body),
        })
        record_history(
            "manual_update",
            record_id=record_id,
            old=old,
            new=dict(record),
            reason="manual update command",
            source="memory_vault.py update",
        )
        save_records(records)
        return
    raise SystemExit(f"No record found for id {record_id}")


@locked_mutation
def set_status(record_id: str, status: str) -> None:
    ensure_layout()
    if status not in VALID_STATUSES:
        raise SystemExit("status must be pending, active, superseded, archived, disputed, or rejected")
    records = read_jsonl(RECORDS_PATH)
    for record in records:
        if record.get("id") == record_id:
            current = str(record.get("status", ""))
            allowed = {
                "active": {"archived", "disputed"},
                "disputed": {"archived"},
                "pending": {"rejected"},
                "archived": set(),
                "superseded": set(),
                "rejected": set(),
            }
            if status == current:
                return
            if status not in allowed.get(current, set()):
                raise SystemExit(
                    f"Unsafe status transition {current}->{status}; "
                    "use promote, merge, or rollback for activation/version changes"
                )
            old = dict(record)
            record["status"] = status
            record["updated_at"] = now_iso()
            record_history(
                "status_change",
                record_id=record_id,
                old=old,
                new=dict(record),
                reason=f"status set to {status}",
                source="memory_vault.py status",
            )
            save_records(records)
            return
    raise SystemExit(f"No record found for id {record_id}")


def duplicate_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [r for r in records if r.get("status") == "active"]
    candidates = []
    for i, left in enumerate(active):
        for right in active[i + 1:]:
            if left.get("topic") != right.get("topic"):
                continue
            a = normalize(left.get("summary", "")).lower()
            b = normalize(right.get("summary", "")).lower()
            if not a or not b:
                continue
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio >= 0.82:
                candidates.append({
                    "left": left["id"],
                    "right": right["id"],
                    "topic": left.get("topic"),
                    "similarity": round(ratio, 3),
                    "left_title": left.get("title", ""),
                    "right_title": right.get("title", ""),
                })
    return candidates


def load_memory_config_provider() -> str:
    config = HERMES_HOME / "config.yaml"
    if not config.exists():
        return ""
    try:
        import yaml

        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        memory = data.get("memory", {}) if isinstance(data, dict) else {}
        provider = memory.get("provider", "") if isinstance(memory, dict) else ""
        return str(provider or "").strip()
    except Exception:
        in_memory = False
        for line in config.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\S", line):
                in_memory = line.strip() == "memory:"
                continue
            if in_memory and re.match(r"\s+provider\s*:", line):
                return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def mem0_readiness() -> dict[str, Any]:
    mem0_json = HERMES_HOME / "mem0.json"
    mem0_config: dict[str, Any] = {}
    if mem0_json.exists():
        try:
            mem0_config = json.loads(mem0_json.read_text(encoding="utf-8"))
        except Exception:
            mem0_config = {}
    vector_config: dict[str, Any] = {}
    try:
        vector_config = (
            mem0_config.get("oss", {})
            .get("vector_store", {})
            .get("config", {})
        )
        if not isinstance(vector_config, dict):
            vector_config = {}
    except Exception:
        vector_config = {}
    cfg_path = vector_config.get("path")
    cfg_url = str(vector_config.get("url", "") or "").rstrip("/")
    vector_path = Path(os.path.expanduser(str(cfg_path))) if cfg_path else None
    vector_mode = "server" if cfg_url else "local"
    vector_url_reachable = False
    vector_url_error = ""
    if cfg_url:
        try:
            with urllib.request.urlopen(f"{cfg_url}/healthz", timeout=2) as response:
                vector_url_reachable = 200 <= int(response.status) < 300
        except Exception as exc:
            vector_url_error = f"{type(exc).__name__}: {exc}"
    ollama = ollama_readiness(mem0_config)
    deps = {
        "mem0": importlib.util.find_spec("mem0") is not None,
        "qdrant_client": importlib.util.find_spec("qdrant_client") is not None,
    }
    if ollama["required"]:
        deps["ollama"] = importlib.util.find_spec("ollama") is not None
    active_provider = load_memory_config_provider()
    vault_local_only = active_provider == "vault"
    vector_ready = vector_url_reachable if cfg_url else True
    ready_for_shadow = (
        not vault_local_only
        and all(deps.values())
        and mem0_json.exists()
        and (not ollama["required"] or ollama["ready"])
        and vector_ready
    )
    return {
        "deps": deps,
        "mem0_json_exists": mem0_json.exists(),
        "mem0_mode": mem0_config.get("mode", "") if mem0_config else "",
        "vector_mode": vector_mode,
        "vector_url": cfg_url,
        "vector_url_reachable": vector_url_reachable,
        "vector_url_error": vector_url_error,
        "vector_path": str(vector_path) if vector_path else "",
        "vector_path_exists": vector_path.exists() if vector_path else False,
        "vector_path_size_bytes": directory_size(vector_path) if vector_path else 0,
        "ollama": ollama,
        "active_memory_provider": active_provider or "(built-in only)",
        "vault_local_only": vault_local_only,
        "ready_for_shadow": ready_for_shadow,
        "ready_for_production": vault_local_only or (ready_for_shadow and active_provider == "mem0"),
    }


def ollama_readiness(mem0_config: dict[str, Any]) -> dict[str, Any]:
    oss = mem0_config.get("oss", {}) if isinstance(mem0_config, dict) else {}
    blocks = {
        "llm": oss.get("llm", {}),
        "embedder": oss.get("embedder", {}),
    }
    required_models: list[str] = []
    for block in blocks.values():
        if not isinstance(block, dict) or block.get("provider") != "ollama":
            continue
        model = str(block.get("config", {}).get("model", "")).strip()
        if model:
            required_models.append(model)
    if not required_models:
        return {"required": False, "ready": True, "models": [], "missing": []}
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2.0) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        installed = [str(item.get("name", "")) for item in payload.get("models", [])]
    except Exception as exc:
        return {
            "required": True,
            "ready": False,
            "models": [],
            "missing": required_models,
            "error": f"{type(exc).__name__}: {exc}",
        }
    installed_bases = {name.split(":", 1)[0] for name in installed}
    missing = [
        model for model in required_models
        if model not in installed and model.split(":", 1)[0] not in installed_bases
    ]
    return {
        "required": True,
        "ready": not missing,
        "models": installed,
        "missing": missing,
    }


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def audit(write_report: bool = True) -> dict[str, Any]:
    ensure_layout()
    records = read_jsonl(RECORDS_PATH)
    active = [r for r in records if r.get("status") == "active"]
    by_topic: dict[str, int] = {}
    by_policy: dict[str, int] = {}
    for record in active:
        by_topic[record.get("topic", "other")] = by_topic.get(record.get("topic", "other"), 0) + 1
        by_policy[record.get("core_policy", "unknown")] = by_policy.get(record.get("core_policy", "unknown"), 0) + 1
    core_chars = {
        "MEMORY.md": len((MEMORIES_DIR / "MEMORY.md").read_text(encoding="utf-8")) if (MEMORIES_DIR / "MEMORY.md").exists() else 0,
        "USER.md": len((MEMORIES_DIR / "USER.md").read_text(encoding="utf-8")) if (MEMORIES_DIR / "USER.md").exists() else 0,
    }
    payload = {
        "generated_at": now_iso(),
        "record_count": len(records),
        "active_count": len(active),
        "topics": by_topic,
        "core_policy": by_policy,
        "core_chars": core_chars,
        "duplicate_candidates": duplicate_candidates(records),
        "mem0": mem0_readiness(),
        "vault_database": database_health(),
        "local_index": local_index_health(records),
        "recommendations": [],
    }
    if core_chars["MEMORY.md"] > 4800:
        payload["recommendations"].append("MEMORY.md is above 80% of the 6000-char target; move archive_candidate entries to the vault.")
    if by_policy.get("archive_candidate", 0):
        payload["recommendations"].append(f"{by_policy['archive_candidate']} active vault records are archive candidates and should not grow Core.")
    if not payload["mem0"]["ready_for_shadow"] and not payload["mem0"].get("vault_local_only"):
        missing = []
        deps = payload["mem0"].get("deps", {})
        if not all(deps.values()):
            missing.append("install mem0 Python dependencies")
        if not payload["mem0"].get("mem0_json_exists"):
            missing.append("create mem0.json")
        ollama = payload["mem0"].get("ollama", {})
        if ollama.get("missing"):
            missing.append("pull Ollama model(s): " + ", ".join(ollama["missing"]))
        if payload["mem0"].get("vector_mode") == "server" and not payload["mem0"].get("vector_url_reachable"):
            missing.append("start Qdrant server at " + str(payload["mem0"].get("vector_url") or "(unset URL)"))
        detail = "; ".join(missing) if missing else "check mem0 config"
        payload["recommendations"].append(f"mem0 OSS is not ready for shadow indexing; {detail}.")
    if not payload["vault_database"].get("healthy"):
        payload["recommendations"].append("Vault database integrity, event chain, or index outbox needs attention.")
    embedding_status = str(
        payload["local_index"].get("embedding_status", "unknown")
    )
    if (
        active
        and not payload["local_index"].get("semantic_ready")
        and not embedding_status.startswith("disabled:")
    ):
        payload["recommendations"].append(
            "Vault semantic retrieval is degraded to lexical-only; "
            f"embedding_status={embedding_status} "
            f"vectors={payload['local_index'].get('vector_records', 0)}/{len(active)}."
        )

    if write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        json_path = REPORT_DIR / f"{stamp}.json"
        md_path = REPORT_DIR / f"{stamp}.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lines = [
            f"# Hermes Memory Audit - {stamp}",
            "",
            f"- Active records: {payload['active_count']}",
            f"- MEMORY.md: {core_chars['MEMORY.md']}/6000 chars",
            f"- USER.md: {core_chars['USER.md']}/3000 chars",
            f"- Duplicate candidates: {len(payload['duplicate_candidates'])}",
            f"- production retrieval: {payload['local_index'].get('mode', 'lexical-only')}",
            f"- embedding status: {payload['local_index'].get('embedding_status', 'unknown')}",
            "",
            "## Topics",
            "",
        ]
        for topic, count in sorted(by_topic.items()):
            lines.append(f"- {topic}: {count}")
        lines.extend(["", "## Recommendations", ""])
        if payload["recommendations"]:
            lines.extend(f"- {item}" for item in payload["recommendations"])
        else:
            lines.append("- None.")
        if payload["duplicate_candidates"]:
            lines.extend(["", "## Duplicate Candidates", ""])
            for item in payload["duplicate_candidates"][:20]:
                lines.append(
                    f"- {item['similarity']}: `{item['left']}` {item['left_title']} <-> "
                    f"`{item['right']}` {item['right_title']}"
                )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload["report_md"] = str(md_path)
        payload["report_json"] = str(json_path)
    return payload


def observe(write_report: bool = True) -> dict[str, Any]:
    """Compatibility entry point for the retired shadow-observe job.

    Observation is now a local audit only.  Keeping this callable avoids
    breaking older operator commands while ensuring it cannot wake mem0 or
    Qdrant after the external index has been retired.
    """
    return audit(write_report=write_report)


def print_records(topic: str | None = None) -> None:
    ensure_layout()
    records = [r for r in read_jsonl(RECORDS_PATH) if r.get("status") == "active"]
    if topic:
        records = [r for r in records if r.get("topic") == topic]
    for record in records:
        print(f"{record['id']} [{record.get('topic')}] {record.get('title')} ({record.get('core_policy')})")


def print_review() -> None:
    ensure_layout()
    records = [
        r for r in read_jsonl(RECORDS_PATH)
        if r.get("status") == "pending"
    ]
    if not records:
        print("No pending memory candidates.")
        return
    records.sort(key=lambda r: (r.get("risk", ""), r.get("created_at", ""), r.get("id", "")))
    for record in records:
        print(
            f"{record['id']} [{record.get('topic')}] risk={record.get('risk', 'unknown')} "
            f"action={record.get('governance_action', '')} review={record.get('review_status', '')} "
            f"matched={record.get('matched_id', '')}"
        )
        print(f"  {record.get('title', '')}")
        print(f"  {record.get('summary', '')}")


def main(argv: list[str] | None = None) -> int:
    ensure_hermes_runtime()
    if argv is None and len(sys.argv) == 1:
        argv = ["audit"]
    parser = argparse.ArgumentParser(description="Manage Max's local Hermes memory vault.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vault directories and empty files.")
    sub.add_parser("import-core", help="Import MEMORY.md and USER.md into the vault.")
    audit_p = sub.add_parser("audit", help="Write a memory audit report.")
    audit_p.add_argument("--json", action="store_true", help="Print JSON payload.")
    observe_p = sub.add_parser("observe", help="Run a local Vault audit (legacy command name).")
    observe_p.add_argument("--json", action="store_true", help="Print JSON payload.")
    list_p = sub.add_parser("list", help="List active vault records.")
    list_p.add_argument("--topic", default="", help="Filter by topic.")
    propose_p = sub.add_parser("propose", help="Propose a durable memory candidate without activating it.")
    propose_p.add_argument("--body", default="", help="Candidate memory body text.")
    propose_p.add_argument("--body-file", default="", help="Read candidate body from a UTF-8 file.")
    propose_p.add_argument("--title", default="", help="Optional title.")
    propose_p.add_argument("--topic", default="", choices=sorted(TOPIC_LABELS), help="Optional topic.")
    propose_p.add_argument("--tag", action="append", default=[], help="Optional tag; repeat or comma-separate.")
    propose_p.add_argument("--source", default="manual", help="Source label for audit history.")
    sub.add_parser("review", help="List pending memory candidates.")
    promote_p = sub.add_parser("promote", help="Promote a pending candidate to active memory.")
    promote_p.add_argument("id")
    promote_p.add_argument("--reason", default="promote candidate")
    promote_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    reject_p = sub.add_parser("reject", help="Reject a pending candidate.")
    reject_p.add_argument("id")
    reject_p.add_argument("--reason", default="reject candidate")
    merge_p = sub.add_parser("merge", help="Merge a pending candidate by superseding an active memory.")
    merge_p.add_argument("candidate_id")
    merge_p.add_argument("active_id")
    merge_p.add_argument("--reason", default="merge candidate into active memory")
    merge_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    rollback_p = sub.add_parser("rollback", help="Rollback a history entry by history_id.")
    rollback_p.add_argument("history_id")
    rollback_p.add_argument("--reason", default="rollback")
    rollback_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    add_p = sub.add_parser("add", help="Add a durable memory to the local authority vault.")
    add_p.add_argument("--body", default="", help="Memory body text.")
    add_p.add_argument("--body-file", default="", help="Read memory body from a UTF-8 file.")
    add_p.add_argument("--title", default="", help="Optional title.")
    add_p.add_argument("--topic", default="", choices=sorted(TOPIC_LABELS), help="Optional topic.")
    add_p.add_argument("--tag", action="append", default=[], help="Optional tag; repeat or comma-separate.")
    add_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    update_p = sub.add_parser("update", help="Update a record by id and preserve old content in history.")
    update_p.add_argument("id")
    update_p.add_argument("--body", default="")
    update_p.add_argument("--body-file", default="")
    update_p.add_argument("--title", default="")
    update_p.add_argument("--topic", default="")
    update_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    status_p = sub.add_parser("status", help="Set record status.")
    status_p.add_argument("id")
    status_p.add_argument("status")
    status_p.add_argument("--no-sync", action="store_true", help="Retained for compatibility; Vault writes are local-only.")
    sub.add_parser("export-mem0", help="Regenerate mem0 seed JSONL from active records.")
    sync_p = sub.add_parser("sync-mem0", help="Retired compatibility command; external indexing is disabled.")
    sync_p.add_argument("--dry-run", action="store_true", help="Show sync scope without touching mem0.")
    sync_p.add_argument("--limit", type=int, default=0, help="Limit records for a test sync.")
    outbox_p = sub.add_parser("sync-index", help="Retired compatibility command; external indexing is disabled.")
    outbox_p.add_argument("--dry-run", action="store_true", help="Show queued work without touching the index.")

    args = parser.parse_args(argv)
    if args.cmd == "init":
        ensure_layout()
        write_index(read_jsonl(RECORDS_PATH))
        print(f"Initialized {VAULT_DIR}")
    elif args.cmd == "import-core":
        added, updated = import_core()
        print(f"Imported core memory: added={added}, updated={updated}, vault={RECORDS_PATH}")
    elif args.cmd == "audit":
        payload = audit(write_report=True)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Memory audit written: {payload.get('report_md')}")
    elif args.cmd == "observe":
        payload = audit(write_report=True)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Memory audit written: {payload.get('report_md')}")
    elif args.cmd == "list":
        print_records(args.topic or None)
    elif args.cmd == "propose":
        body = args.body
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8")
        if not body.strip():
            raise SystemExit("Provide --body or --body-file")
        record, added = propose_record(
            body,
            title=args.title or None,
            topic=args.topic or None,
            tags=args.tag,
            source=args.source,
        )
        verb = "Proposed" if added else "Already exists"
        print(
            f"{verb} {record['id']}: status={record.get('status')} "
            f"review={record.get('review_status', '')} action={record.get('governance_action', '')} "
            f"risk={record.get('risk', '')} matched={record.get('matched_id', '')}"
        )
    elif args.cmd == "review":
        print_review()
    elif args.cmd == "promote":
        record = promote_record(args.id, reason=args.reason)
        print(f"Promoted {record['id']}: {record.get('title', '')}")
    elif args.cmd == "reject":
        record = reject_record(args.id, reason=args.reason)
        print(f"Rejected {record['id']}: {record.get('title', '')}")
    elif args.cmd == "merge":
        record = merge_record(args.candidate_id, args.active_id, reason=args.reason)
        print(f"Merged {args.candidate_id} over {args.active_id}: active={record['id']}")
    elif args.cmd == "rollback":
        entry = rollback_history(args.history_id, reason=args.reason)
        print(f"Rolled back {args.history_id}: {entry['history_id']}")
    elif args.cmd == "add":
        body = args.body
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8")
        if not body.strip():
            raise SystemExit("Provide --body or --body-file")
        record, added = add_record(
            body,
            title=args.title or None,
            topic=args.topic or None,
            tags=args.tag,
        )
        verb = "Added" if added else "Already exists"
        print(f"{verb} {record['id']}: {record.get('title', '')}")
    elif args.cmd == "update":
        body = args.body
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8")
        if not body.strip():
            raise SystemExit("Provide --body or --body-file")
        update_record(args.id, body.strip(), title=args.title or None, topic=args.topic or None)
        print(f"Updated {args.id}")
    elif args.cmd == "status":
        set_status(args.id, args.status)
        print(f"Set {args.id} status={args.status}")
    elif args.cmd == "export-mem0":
        ensure_layout()
        records = read_jsonl(RECORDS_PATH)
        write_mem0_seed(records)
        print(f"Wrote {MEM0_SEED_PATH}")
    elif args.cmd == "sync-mem0":
        print("External mem0/Qdrant indexing is retired; Vault is local-only. No action taken.")
        return 2
    elif args.cmd == "sync-index":
        print("External mem0/Qdrant index replay is retired; Vault is local-only. No action taken.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
