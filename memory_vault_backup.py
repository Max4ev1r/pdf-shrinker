#!/usr/bin/env python3
"""Create and verify local-only, multi-generation Hermes memory vault backups."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def find_shared_home(data_home: Path) -> Path:
    """Locate shared Vault code when data lives under a Hermes profile."""
    for candidate in (data_home, *data_home.parents):
        if (
            (candidate / "scripts" / "memory_vault.py").is_file()
            and (candidate / "plugins" / "vault" / "plugin.yaml").is_file()
        ):
            return candidate
    return Path.home() / ".hermes"


SHARED_HOME = find_shared_home(HERMES_HOME)
VAULT_DIR = HERMES_HOME / "memory-vault"
BACKUP_DIR = HERMES_HOME / "backups" / "memory-vault"
CONFIG_PATH = HERMES_HOME / "config.yaml"
PLUGIN_DIR = SHARED_HOME / "plugins" / "vault"
SCRIPTS_DIR = SHARED_HOME / "scripts"
VAULT_LIBRARY_DIR = SCRIPTS_DIR / "memory_vault_lib"
CRITICAL_SCRIPT_NAMES = {
    "memory_vault.py",
    "memory_governor.py",
    "memory_retrieval_eval.py",
    "memory_vault_backup.py",
    "memory_vault_backup_job.py",
    "memory_vault_restore_verify_job.py",
    "memory_integrity_check.py",
    "memory_doctor.py",
}
VALID_STATUSES = {
    "pending", "active", "superseded", "archived", "disputed", "rejected",
}


def stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_homes() -> list[Path]:
    """The scheduled root backup includes each profile's own history and Core."""
    homes = [HERMES_HOME]
    profiles = HERMES_HOME / "profiles"
    if profiles.is_dir():
        homes.extend(p for p in sorted(profiles.iterdir()) if p.is_dir() and not p.is_symlink())
    return homes


SQLITE_SKIP_NAMES = {
    "local-search.sqlite3", "vault.sqlite3", "vault.sqlite3-wal", "vault.sqlite3-shm", "vault.lock",
}


def _walk_files(root: Path, *, skip_names: set[str] | None = None) -> list[Path]:
    skip = skip_names or set()
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in skip or "__pycache__" in path.parts:
            continue
        out.append(path)
    return out


def class_a_sqlite_paths() -> list[Path]:
    """Authoritative SQLite databases captured via the backup API."""
    paths: list[Path] = []
    for home in profile_homes():
        vault = home / "memory-vault"
        for name in ("vault.sqlite3", "local-search.sqlite3"):
            path = vault / name
            if path.exists():
                paths.append(path)
        state = home / "state.db"
        if state.exists():
            paths.append(state)
    return paths


def source_files() -> list[Path]:
    files: list[Path] = []
    # Root vault portable/export sidecars (SQLite handled separately).
    files.extend(_walk_files(VAULT_DIR, skip_names=SQLITE_SKIP_NAMES))
    # Specialist / domain memory (Class A, not derived).
    files.extend(_walk_files(HERMES_HOME / "expert-memory", skip_names={".current_sessions.json.lock"}))
    if CONFIG_PATH.exists():
        files.append(CONFIG_PATH)
    for home in profile_homes():
        for name in ("config.yaml", "SOUL.md", "memories/USER.md", "memories/MEMORY.md"):
            path = home / name
            if path.is_file():
                files.append(path)
        files.extend((home / "pending" / "memory").glob("*.json"))
        # Profile vault portable exports (SQLite via class_a_sqlite_paths).
        if home != HERMES_HOME:
            files.extend(_walk_files(home / "memory-vault", skip_names=SQLITE_SKIP_NAMES))
    for name in CRITICAL_SCRIPT_NAMES:
        path = SCRIPTS_DIR / name
        if path.exists():
            files.append(path)
    tests_dir = SCRIPTS_DIR / "tests"
    if tests_dir.exists():
        files.extend(
            path for path in tests_dir.rglob("test_*.py")
            if path.is_file()
        )
    if VAULT_LIBRARY_DIR.exists():
        files.extend(
            path for path in VAULT_LIBRARY_DIR.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
    if PLUGIN_DIR.exists():
        files.extend(
            path for path in PLUGIN_DIR.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return sorted(set(files))


def archive_relative_path(path: Path) -> Path:
    for root in (HERMES_HOME, SHARED_HOME):
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    raise RuntimeError(f"Backup source is outside Hermes homes: {path}")


def archive_name() -> str:
    return f"vault-{stamp()}.tar.gz"


def create_snapshot() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    sources = source_files()
    if not sources:
        raise RuntimeError("No vault files found to back up")
    name = archive_name()
    backup_id = name.removesuffix(".tar.gz")
    archive = BACKUP_DIR / name
    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "archive": name,
        "files": [],
        "components": [],
        "session_databases": {},
        "profile_plugin_links": {},
        "class_a_components": [
            "memory-vault/vault.sqlite3",
            "memories/MEMORY.md",
            "memories/USER.md",
            "profiles/*/memory-vault/vault.sqlite3",
            "expert-memory/**",
            "pending/memory/**",
            "scripts/memory_vault*.py",
            "scripts/memory_vault_lib/**",
            "plugins/vault/**",
        ],
    }
    for home in profile_homes():
        for link in (home / "plugins").glob("*"):
            if link.is_symlink():
                manifest["profile_plugin_links"][str(archive_relative_path(link))] = os.readlink(link)
    with tempfile.TemporaryDirectory(prefix="hermes-vault-snapshot-") as raw_snapshot:
        import sqlite3

        snapshot_relatives: dict[Path, Path] = {}
        for db_source in class_a_sqlite_paths():
            if not db_source.exists():
                continue
            relative = archive_relative_path(db_source)
            db_snapshot = Path(raw_snapshot) / relative
            db_snapshot.parent.mkdir(parents=True, exist_ok=True)
            component: dict[str, Any] = {
                "component": str(relative),
                "source_path": str(db_source),
                "kind": "sqlite",
            }
            with sqlite3.connect(f"{db_source.as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(db_snapshot) as target:
                source.backup(target)
                if db_source.name == "state.db":
                    manifest["session_databases"][str(relative)] = {
                        table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in ("sessions", "messages")
                    }
                    component["counts"] = manifest["session_databases"][str(relative)]
                elif db_source.name == "vault.sqlite3":
                    counts = {}
                    for table in ("records", "events", "evidence"):
                        try:
                            counts[table] = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        except Exception:
                            counts[table] = None
                    try:
                        meta = dict(target.execute("SELECT key,value FROM meta").fetchall())
                        component["schema_version"] = meta.get("schema_version")
                    except Exception:
                        component["schema_version"] = None
                    component["counts"] = counts
                    manifest["components"].append(component)
                elif db_source.name == "local-search.sqlite3":
                    try:
                        meta = dict(target.execute("SELECT key,value FROM index_meta").fetchall())
                        component["index_meta"] = meta
                        component["counts"] = {
                            "memories": target.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                            "vectors": target.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0],
                        }
                    except Exception:
                        pass
                    manifest["components"].append(component)
            sources.append(db_snapshot)
            snapshot_relatives[db_snapshot] = relative
        tmp_archive.touch(mode=0o600, exist_ok=False)
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for path in sources:
                relative = snapshot_relatives.get(path) or archive_relative_path(path)
                tar.add(path, arcname=str(relative), recursive=False)
                source_path = str(path)
                for snap, orig_rel in snapshot_relatives.items():
                    if path == snap or path.name == Path(orig_rel).name and str(orig_rel) == str(relative):
                        source_path = str(orig_rel)
                        break
                manifest["files"].append({
                    "path": str(relative),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                    "source_path": source_path,
                })
    os.replace(tmp_archive, archive)
    manifest["archive_sha256"] = sha256_file(archive)
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    manifest_path.touch(mode=0o600, exist_ok=False)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prune_snapshots()
    return {"archive": str(archive), "manifest": str(manifest_path), "files": len(manifest["files"])}


def snapshot_pairs() -> list[tuple[Path, Path]]:
    pairs = []
    for manifest in BACKUP_DIR.glob("vault-*.tar.gz.manifest.json"):
        archive = manifest.with_name(manifest.name.removesuffix(".manifest.json"))
        if archive.exists():
            pairs.append((archive, manifest))
    return sorted(pairs, key=lambda pair: pair[0].name)


def prune_snapshots() -> None:
    pairs = snapshot_pairs()
    today = dt.datetime.now().date()
    keep: set[Path] = set()
    monthly: set[str] = set()
    yearly: set[str] = set()
    for archive, manifest in reversed(pairs):
        try:
            when = dt.datetime.strptime(archive.name[6:21], "%Y%m%d-%H%M%S").date()
        except ValueError:
            keep.add(archive)
            continue
        age = (today - when).days
        if age <= 30:
            keep.add(archive)
        elif age <= 365:
            key = when.strftime("%Y-%m")
            if key not in monthly and len(monthly) < 12:
                keep.add(archive)
                monthly.add(key)
        else:
            key = when.strftime("%Y")
            if key not in yearly and len(yearly) < 10:
                keep.add(archive)
                yearly.add(key)
    for archive, manifest in pairs:
        if archive not in keep:
            archive.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)


def verify_snapshot(archive: Path) -> dict[str, Any]:
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(archive) != manifest.get("archive_sha256"):
        raise RuntimeError("Archive SHA-256 mismatch")
    with tempfile.TemporaryDirectory(prefix="hermes-vault-restore-") as raw:
        root = Path(raw)
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
                raise RuntimeError("Archive contains unsafe path")
            tar.extractall(root, filter="data")
        for item in manifest.get("files", []):
            restored = root / item["path"]
            if not restored.exists() or sha256_file(restored) != item["sha256"]:
                raise RuntimeError(f"Restored file failed verification: {item['path']}")
        restored_histories = {}
        for relative, expected_counts in manifest.get("session_databases", {}).items():
            import sqlite3
            with sqlite3.connect(root / relative) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"Restored session database is corrupt: {relative}")
                counts = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("sessions", "messages")
                }
                if counts != expected_counts:
                    raise RuntimeError(f"Restored history count mismatch: {relative}")
                restored_histories[relative] = counts
        records = root / "memory-vault" / "memories.jsonl"
        if records.exists():
            for line in records.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)
        database = root / "memory-vault" / "vault.sqlite3"
        integrity = "missing"
        record_count = 0
        event_count = 0
        active_count = 0
        if database.exists():
            import sqlite3
            with sqlite3.connect(database) as conn:
                conn.row_factory = sqlite3.Row
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
                record_count = int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])
                active_count = int(
                    conn.execute("SELECT COUNT(*) FROM records WHERE status='active'").fetchone()[0]
                )
                record_rows = conn.execute("SELECT id,status,data FROM records").fetchall()
                record_ids = [str(row["id"]) for row in record_rows]
                if len(record_ids) != len(set(record_ids)):
                    raise RuntimeError("Restored vault contains duplicate record IDs")
                invalid_statuses = sorted({
                    str(row["status"]) for row in record_rows
                    if row["status"] not in VALID_STATUSES
                })
                if invalid_statuses:
                    raise RuntimeError(
                        "Restored vault contains invalid statuses: "
                        + ", ".join(invalid_statuses)
                    )
                active_ids = {
                    str(row["id"]) for row in record_rows
                    if row["status"] == "active"
                }
                for row in record_rows:
                    if row["status"] != "active":
                        continue
                    data = json.loads(row["data"])
                    source = data.get("source", {})
                    predecessor = (
                        str(source.get("supersedes", ""))
                        if isinstance(source, dict)
                        else ""
                    )
                    if not predecessor and data.get("governance_action") == "merge":
                        predecessor = str(data.get("matched_id", ""))
                    if predecessor and predecessor in active_ids:
                        raise RuntimeError(
                            f"Restored vault has two active versions: {row['id']} and {predecessor}"
                        )
                event_rows = conn.execute(
                    "SELECT previous_hash,event_hash,event_json FROM events ORDER BY seq"
                ).fetchall()
                previous = "0" * 64
                for row in event_rows:
                    if row["previous_hash"] != previous:
                        raise RuntimeError("Vault event hash chain has a broken previous_hash")
                    event = json.loads(row["event_json"])
                    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    expected = hashlib.sha256((previous + canonical).encode("utf-8")).hexdigest()
                    if row["event_hash"] != expected:
                        raise RuntimeError("Vault event hash chain verification failed")
                    previous = expected
                event_count = len(event_rows)
        if records.exists():
            export_count = sum(
                1 for line in records.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if database.exists() and export_count != record_count:
                raise RuntimeError(
                    f"Portable record export count {export_count} does not match SQLite {record_count}"
                )
        index_path = root / "memory-vault" / "index.json"
        if index_path.exists() and database.exists():
            index_payload = json.loads(index_path.read_text(encoding="utf-8"))
            if int(index_payload.get("record_count", -1)) != record_count:
                raise RuntimeError("Restored index.json record count does not match SQLite")
            if int(index_payload.get("active_count", -1)) != active_count:
                raise RuntimeError("Restored index.json active count does not match SQLite")
        local_index = root / "memory-vault" / "local-search.sqlite3"
        local_index_integrity = "missing"
        indexed_count = 0
        vector_count = 0
        embedding_status = "missing"
        if local_index.exists():
            import sqlite3
            with sqlite3.connect(local_index) as conn:
                local_index_integrity = str(
                    conn.execute("PRAGMA integrity_check").fetchone()[0]
                )
                if local_index_integrity != "ok":
                    raise RuntimeError(
                        "Local search SQLite integrity_check failed: "
                        + local_index_integrity
                    )
                indexed_count = int(
                    conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                )
                vector_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memory_vectors"
                    ).fetchone()[0]
                )
                metadata = dict(
                    conn.execute("SELECT key,value FROM index_meta").fetchall()
                )
                embedding_status = metadata.get("embedding_status", "unknown")
            if database.exists() and indexed_count != active_count:
                raise RuntimeError(
                    f"Local search record count {indexed_count} does not match active Vault records {active_count}"
                )
        required_code = [
            root / "scripts" / "memory_vault.py",
            root / "scripts" / "memory_vault_backup.py",
            root / "scripts" / "memory_vault_lib" / "__init__.py",
            root / "scripts" / "memory_vault_lib" / "governance.py",
            root / "scripts" / "memory_vault_lib" / "local_index.py",
            root / "plugins" / "vault" / "__init__.py",
            root / "plugins" / "vault" / "plugin.yaml",
            root / "config.yaml",
        ]
        missing_code = [str(path.relative_to(root)) for path in required_code if not path.exists()]
        if missing_code:
            raise RuntimeError(
                "Restored snapshot is missing runtime files: " + ", ".join(missing_code)
            )
        for source in [
            root / "scripts" / name
            for name in CRITICAL_SCRIPT_NAMES
            if (root / "scripts" / name).exists()
        ] + list((root / "scripts" / "memory_vault_lib").glob("*.py")) + [
            root / "plugins" / "vault" / "__init__.py"
        ]:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
    return {
        "archive": str(archive), "verified": True,
        "files": len(manifest.get("files", [])),
        "sqlite_integrity": integrity, "record_count": record_count,
        "active_count": active_count, "event_count": event_count,
        "local_index_integrity": local_index_integrity,
        "indexed_count": indexed_count, "vector_count": vector_count,
        "embedding_status": embedding_status,
        "session_databases": restored_histories,
        "profile_plugin_links": manifest.get("profile_plugin_links", {}),
    }


def resolve_offsite_dir() -> Path:
    """Canonical offsite destination: env > config.yaml memory.offsite_dir > .env."""
    env = os.environ.get("HERMES_MEMORY_OFFSITE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    config = HERMES_HOME / "config.yaml"
    if config.exists():
        try:
            text = config.read_text(encoding="utf-8")
            in_memory = False
            for line in text.splitlines():
                if line.startswith("memory:"):
                    in_memory = True
                    continue
                if in_memory and line[:1] not in (" ", "\t"):
                    in_memory = False
                    continue
                if in_memory and "offsite_dir:" in line:
                    raw = line.split(":", 1)[1].strip().strip("'\"")
                    if raw:
                        return Path(raw).expanduser()
        except Exception:
            pass
    env_file = HERMES_HOME / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("HERMES_MEMORY_OFFSITE_DIR="):
                raw = line.split("=", 1)[1].strip().strip("'\"")
                if raw:
                    return Path(raw).expanduser()
    return HERMES_HOME / "offsite" / "memory-vault"


def offsite_ready_generations(dest: Path) -> list[dict[str, Any]]:
    """Only READY/COMPLETE generations are restore-eligible."""
    dest = dest.expanduser()
    if not dest.exists():
        return []
    out: list[dict[str, Any]] = []
    for ready in sorted(dest.glob("vault-*.tar.gz.ready.json")):
        archive = ready.with_name(ready.name.removesuffix(".ready.json"))
        manifest = archive.with_name(archive.name + ".manifest.json")
        checksum = dest / (archive.name + ".sha256")
        if not archive.exists():
            continue
        try:
            ready_doc = json.loads(ready.read_text(encoding="utf-8"))
        except Exception:
            continue
        if ready_doc.get("state") != "complete":
            continue
        out.append({
            "archive": archive,
            "manifest": manifest,
            "checksum": checksum,
            "ready": ready,
            "backup_id": ready_doc.get("backup_id") or archive.name.removesuffix(".tar.gz"),
            "created_at": ready_doc.get("created_at", ""),
        })
    return out


def prune_offsite(dest: Path) -> dict[str, Any]:
    """Same 30d/12m/10y policy; never deletes the newest READY generation."""
    pairs = offsite_ready_generations(dest)
    if not pairs:
        return {"kept": 0, "removed": 0}
    today = dt.datetime.now().date()
    keep: set[Path] = {pairs[-1]["archive"]}  # always keep latest known-good READY
    monthly: set[str] = set()
    yearly: set[str] = set()
    for item in reversed(pairs):
        archive = item["archive"]
        try:
            when = dt.datetime.strptime(archive.name[6:21], "%Y%m%d-%H%M%S").date()
        except ValueError:
            keep.add(archive)
            continue
        age = (today - when).days
        if age <= 30 or archive in keep:
            keep.add(archive)
        elif age <= 365:
            key = when.strftime("%Y-%m")
            if key not in monthly and len(monthly) < 12:
                keep.add(archive)
                monthly.add(key)
        else:
            key = when.strftime("%Y")
            if key not in yearly and len(yearly) < 10:
                keep.add(archive)
                yearly.add(key)
    removed = 0
    for item in pairs:
        archive = item["archive"]
        if archive in keep:
            continue
        for path in (
            archive,
            item["manifest"],
            item["checksum"],
            item["ready"],
            archive.with_name(archive.name + ".partial.json"),
        ):
            path.unlink(missing_ok=True)
        removed += 1
    return {"kept": len(keep), "removed": removed}


def verify_offsite_generation(item: dict[str, Any]) -> dict[str, Any]:
    archive: Path = item["archive"]
    manifest_path: Path = item["manifest"]
    checksum_path: Path = item["checksum"]
    if not archive.exists() or not manifest_path.exists():
        return {"valid": False, "reason": "archive_or_manifest_missing"}
    if not item.get("ready") or not Path(item["ready"]).exists():
        return {"valid": False, "reason": "ready_marker_missing"}
    try:
        ready_doc = json.loads(Path(item["ready"]).read_text(encoding="utf-8"))
        if ready_doc.get("state") != "complete":
            return {"valid": False, "reason": "ready_state_not_complete"}
    except Exception as e:
        return {"valid": False, "reason": f"ready_parse:{e}"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"valid": False, "reason": f"manifest_parse:{e}"}
    digest = sha256_file(archive)
    if manifest.get("archive_sha256") != digest:
        return {"valid": False, "reason": "archive_sha256_mismatch"}
    if checksum_path.exists():
        claimed = checksum_path.read_text(encoding="utf-8").split()[0]
        if claimed != digest:
            return {"valid": False, "reason": "sidecar_sha256_mismatch"}
    return {
        "valid": True,
        "backup_id": item.get("backup_id"),
        "archive_sha256": digest,
        "manifest_class_a": manifest.get("class_a_components", []),
        "components": manifest.get("components", []),
    }


def icloud_sync_status(path: Path) -> dict[str, Any]:
    """iCloud/FileProvider status. Remote durability only if isUploaded=1."""
    import re
    import subprocess

    path = Path(path)
    result = {
        "path": str(path),
        "local_file_present": path.exists(),
        "upload_pending": None,
        "sync_error": None,
        "remote_durability_verified": False,
        "method": [],
        "detail": {},
    }
    if not path.exists():
        result["sync_error"] = "missing_locally"
        return result
    try:
        st = path.stat()
        result["detail"]["st_size"] = st.st_size
        result["detail"]["st_blocks"] = getattr(st, "st_blocks", None)
    except Exception:
        pass
    # Authoritative: fileproviderctl evaluate (iCloud Drive File Provider item state)
    try:
        proc = subprocess.run(
            ["fileproviderctl", "evaluate", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        result["method"].append("fileproviderctl.evaluate")
        blob = proc.stdout + "\n" + proc.stderr
        result["detail"]["evaluate_rc"] = proc.returncode
        flags = {}
        for key in (
            "isUploaded",
            "isUploading",
            "isDownloaded",
            "isDownloading",
            "hasUnresolvedConflicts",
            "isSyncPaused",
            "isExcludedFromSync",
            "isMostRecentVersionDownloaded",
        ):
            m = re.search(rf"{key}\s*=\s*([01])", blob)
            if m:
                flags[key] = m.group(1) == "1"
        result["detail"]["file_provider_flags"] = flags
        if flags:
            result["upload_pending"] = bool(flags.get("isUploading")) or (flags.get("isUploaded") is False)
            result["sync_error"] = bool(flags.get("hasUnresolvedConflicts")) or bool(flags.get("isSyncPaused"))
            if flags.get("isUploaded") is True and not flags.get("isUploading") and not flags.get("hasUnresolvedConflicts"):
                result["remote_durability_verified"] = True
                result["upload_pending"] = False
            elif flags.get("isUploaded") is False:
                result["remote_durability_verified"] = False
        else:
            result["detail"]["evaluate_raw"] = blob[:800]
    except Exception as e:
        result["detail"]["fileprovider_error"] = f"{type(e).__name__}: {e}"
    return result


def export_offsite(dest: Path, *, archive: Path | None = None) -> dict[str, Any]:
    """Atomically publish one verified generation to a failure-domain split path.

    Publish order: staging files → manifest → checksum → READY(state=complete).
    Incomplete generations never get READY and are not restore-eligible.
    """
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        dest.chmod(0o700)
    except Exception:
        pass
    if archive is None:
        pairs = snapshot_pairs()
        if not pairs:
            raise RuntimeError("No vault snapshot exists to export")
        archive = pairs[-1][0]
    verify_snapshot(archive)
    manifest_path = archive.with_name(archive.name + ".manifest.json")
    staging = dest / f".staging-{archive.stem}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        staged_archive = staging / archive.name
        staged_manifest = staging / manifest_path.name
        staged_checksum = staging / (archive.name + ".sha256")
        staged_ready = staging / (archive.name + ".ready.json")
        shutil.copy2(archive, staged_archive)
        shutil.copy2(manifest_path, staged_manifest)
        checksum = sha256_file(staged_archive)
        manifest = json.loads(staged_manifest.read_text(encoding="utf-8"))
        if manifest.get("archive_sha256") != checksum:
            raise RuntimeError("Staged archive checksum mismatch before publish")
        staged_checksum.write_text(checksum + "  " + archive.name + "\n", encoding="utf-8")
        # READY is written last in staging, then published last.
        ready_doc = {
            "state": "complete",
            "backup_id": manifest.get("backup_id") or archive.name.removesuffix(".tar.gz"),
            "created_at": manifest.get("created_at") or dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "archive": archive.name,
            "archive_sha256": checksum,
            "class_a_components": manifest.get("class_a_components", []),
            "components": manifest.get("components", []),
        }
        staged_ready.write_text(json.dumps(ready_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Publish: archive/manifest/checksum first, READY last (atomic visibility gate).
        os.replace(staging / archive.name, dest / archive.name)
        os.replace(staging / manifest_path.name, dest / manifest_path.name)
        os.replace(staging / (archive.name + ".sha256"), dest / (archive.name + ".sha256"))
        os.replace(staging / (archive.name + ".ready.json"), dest / (archive.name + ".ready.json"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    prune_offsite(dest)
    item = next(
        (g for g in offsite_ready_generations(dest) if g["archive"].name == archive.name),
        None,
    )
    if item is None:
        raise RuntimeError("Published generation is not READY")
    check = verify_offsite_generation(item)
    if not check.get("valid"):
        raise RuntimeError(f"Published generation failed verification: {check}")
    sync = icloud_sync_status(dest / archive.name)
    return {
        "offsite_archive": str(dest / archive.name),
        "offsite_manifest": str(dest / manifest_path.name),
        "offsite_ready": str(dest / (archive.name + ".ready.json")),
        "offsite_sha256": checksum,
        "source_archive": str(archive),
        "backup_id": ready_doc["backup_id"],
        "verify": check,
        "sync": sync,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up and verify Hermes memory vault snapshots.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true")
    group.add_argument("--verify-latest", action="store_true")
    group.add_argument("--verify", default="", metavar="ARCHIVE")
    group.add_argument("--export-offsite", default="", metavar="DEST_DIR")
    group.add_argument("--offsite-status", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        result = create_snapshot()
    elif args.export_offsite:
        result = export_offsite(Path(args.export_offsite) if args.export_offsite else resolve_offsite_dir())
    elif args.offsite_status:
        dest = resolve_offsite_dir()
        gens = offsite_ready_generations(dest)
        checks = [verify_offsite_generation(g) for g in gens[-3:]]
        result = {
            "dest": str(dest),
            "generations": [g["backup_id"] for g in gens],
            "latest_verify": checks[-1] if checks else None,
            "sync": icloud_sync_status(dest),
        }
    else:
        archive = Path(args.verify).expanduser() if args.verify else (snapshot_pairs()[-1][0] if snapshot_pairs() else None)
        if archive is None:
            raise SystemExit("No vault snapshot exists")
        result = verify_snapshot(archive)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
