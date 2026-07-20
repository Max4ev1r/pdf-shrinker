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
VAULT_DIR = HERMES_HOME / "memory-vault"
BACKUP_DIR = HERMES_HOME / "backups" / "memory-vault"
CONFIG_PATH = HERMES_HOME / "config.yaml"
MEM0_CONFIG_PATH = HERMES_HOME / "mem0.json"
PLUGIN_DIR = HERMES_HOME / "plugins" / "vault"
SCRIPTS_DIR = HERMES_HOME / "scripts"
VAULT_LIBRARY_DIR = SCRIPTS_DIR / "memory_vault_lib"
CRITICAL_SCRIPT_NAMES = {
    "memory_vault.py",
    "memory_governor.py",
    "memory_retrieval_eval.py",
    "memory_vault_backup.py",
    "memory_vault_backup_job.py",
    "memory_vault_restore_verify_job.py",
    "memory_vault_index_job.py",
    "hindsight_shadow_check.py",
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


def source_files() -> list[Path]:
    files: list[Path] = []
    if VAULT_DIR.exists():
        files.extend(
            p for p in VAULT_DIR.rglob("*")
            if p.is_file() and p.name not in {
                "local-search.sqlite3", "vault.sqlite3", "vault.sqlite3-wal", "vault.sqlite3-shm", "vault.lock",
            }
        )
    if CONFIG_PATH.exists():
        files.append(CONFIG_PATH)
    if MEM0_CONFIG_PATH.exists():
        files.append(MEM0_CONFIG_PATH)
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


def archive_name() -> str:
    return f"vault-{stamp()}.tar.gz"


def create_snapshot() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    sources = source_files()
    if not sources:
        raise RuntimeError("No vault files found to back up")
    name = archive_name()
    archive = BACKUP_DIR / name
    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
    manifest: dict[str, Any] = {"created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "archive": name, "files": []}
    with tempfile.TemporaryDirectory(prefix="hermes-vault-snapshot-") as raw_snapshot:
        db_source = VAULT_DIR / "vault.sqlite3"
        db_snapshot = Path(raw_snapshot) / "vault.sqlite3"
        if db_source.exists():
            import sqlite3
            with sqlite3.connect(db_source) as source, sqlite3.connect(db_snapshot) as target:
                source.backup(target)
            sources.append(db_snapshot)
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for path in sources:
                relative = Path("memory-vault/vault.sqlite3") if path == db_snapshot else path.relative_to(HERMES_HOME)
                tar.add(path, arcname=str(relative), recursive=False)
                manifest["files"].append({"path": str(relative), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    os.replace(tmp_archive, archive)
    manifest["archive_sha256"] = sha256_file(archive)
    manifest_path = archive.with_name(archive.name + ".manifest.json")
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
        required_code = [
            root / "scripts" / "memory_vault.py",
            root / "scripts" / "memory_vault_backup.py",
            root / "scripts" / "memory_vault_lib" / "__init__.py",
            root / "scripts" / "memory_vault_lib" / "governance.py",
            root / "scripts" / "memory_vault_lib" / "local_index.py",
            root / "plugins" / "vault" / "__init__.py",
            root / "plugins" / "vault" / "plugin.yaml",
            root / "mem0.json",
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
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up and verify Hermes memory vault snapshots.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", action="store_true")
    group.add_argument("--verify-latest", action="store_true")
    group.add_argument("--verify", default="", metavar="ARCHIVE")
    args = parser.parse_args()
    if args.snapshot:
        result = create_snapshot()
    else:
        archive = Path(args.verify).expanduser() if args.verify else (snapshot_pairs()[-1][0] if snapshot_pairs() else None)
        if archive is None:
            raise SystemExit("No vault snapshot exists")
        result = verify_snapshot(archive)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
