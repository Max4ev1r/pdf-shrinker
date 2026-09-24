#!/usr/bin/env python3
"""Canonical automated Memory integrity check (daily light / alert-on-fail).

Reuses memory_vault.database_health / local_index_health — this is the
scheduled enforcement entrypoint, not a second diagnostic philosophy.

Run: python3 ~/.hermes/scripts/memory_integrity_check.py [--json]
Exit 0 = healthy (silent). Exit 1 = alert.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_memory_vault():
    path = Path.home() / ".hermes" / "scripts" / "memory_vault.py"
    spec = importlib.util.spec_from_file_location("_memory_vault_integrity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set_diff(active: set[str], vectors: set[str]) -> dict:
    return {
        "missing_vectors": sorted(active - vectors),
        "orphan_vectors": sorted(vectors - active),
    }


def run_checks(hermes_home: Path | None = None) -> dict:
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    mv = _load_memory_vault()
    mv.configure_home(home)
    findings: list[dict] = []
    ok_flags: dict[str, bool] = {}

    def fail(code: str, detail: str, **extra):
        findings.append({"code": code, "detail": detail, **extra})
        ok_flags[code] = False

    def ok(code: str):
        ok_flags[code] = True

    # DB integrity + event chain
    try:
        health = mv.database_health()
        if health.get("integrity") != "ok":
            fail("db_integrity", health.get("integrity", "unknown"))
        else:
            ok("db_integrity")
        if not health.get("event_chain_valid", False):
            fail("event_hash_chain", "broken")
        else:
            ok("event_hash_chain")
    except Exception as e:
        health = {}
        fail("db_integrity", f"{type(e).__name__}: {e}")

    records = mv.read_jsonl(mv.RECORDS_PATH)
    active = {str(r.get("id")) for r in records if r.get("status") == "active"}
    active_n = len(active)
    record_n = len(records)
    idx_health: dict = {}
    vector_ids: set[str] = set()

    # evidence
    try:
        evidences = mv.read_evidence_export()
        evidence_n = len(evidences)
        ok("evidence_present")
    except Exception as e:
        evidence_n = -1
        fail("evidence_present", f"{type(e).__name__}: {e}")

    # schema
    try:
        with sqlite3.connect(f"file:{mv.VAULT_DB_PATH}?mode=ro", uri=True) as conn:
            meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
        stored = int(meta.get("schema_version", "0"))
        if stored > mv.CURRENT_SUPPORTED_SCHEMA:
            fail("schema_version", f"future schema {stored} > {mv.CURRENT_SUPPORTED_SCHEMA}")
        elif stored < 1:
            fail("schema_version", f"invalid schema {stored}")
        else:
            ok("schema_version")
    except Exception as e:
        fail("schema_version", f"{type(e).__name__}: {e}")

    # index set-diff + embedding identity
    try:
        idx_health = mv.local_index_health(records)
        if not idx_health.get("fingerprint_matches", False):
            fail("index_fingerprint", "stale index vs active records")
        else:
            ok("index_fingerprint")
        with sqlite3.connect(f"file:{mv.LOCAL_INDEX_PATH}?mode=ro", uri=True) as conn:
            vector_ids = {r[0] for r in conn.execute("SELECT id FROM memory_vectors")}
            imeta = dict(conn.execute("SELECT key,value FROM index_meta").fetchall())
        diff = _set_diff(active, vector_ids)
        if diff["missing_vectors"]:
            fail("vector_set_diff", "missing vectors", ids=diff["missing_vectors"][:20], count=len(diff["missing_vectors"]))
        elif diff["orphan_vectors"]:
            fail("vector_set_diff", "orphan vectors", ids=diff["orphan_vectors"][:20], count=len(diff["orphan_vectors"]))
        else:
            ok("vector_set_diff")
        mgr = mv._local_index_manager()
        mismatches = mgr.identity_mismatch(imeta)
        if mismatches:
            fail("embedding_identity", "NEEDS_REINDEX", mismatches=mismatches, stored=imeta)
        else:
            ok("embedding_identity")
        if idx_health.get("semantic_ready") is not True and active_n:
            # capability vs metadata: require vectors present and model match
            fail("semantic_ready", idx_health.get("embedding_status", "unknown"), health=idx_health)
        else:
            ok("semantic_ready")
    except Exception as e:
        fail("index_health", f"{type(e).__name__}: {e}")

    # backup freshness (daily expected)
    backup_dir = home / "backups" / "memory-vault"
    pairs = sorted(backup_dir.glob("vault-*.tar.gz"))
    latest = pairs[-1] if pairs else None
    backup = {"latest": str(latest) if latest else None, "count": len(pairs)}
    if not latest:
        fail("backup_freshness", "no backups")
    else:
        age_h = (datetime.now().astimezone() - datetime.fromtimestamp(latest.stat().st_mtime).astimezone()).total_seconds() / 3600
        backup["age_hours"] = round(age_h, 2)
        if age_h > 48:
            fail("backup_freshness", f"latest backup age {age_h:.1f}h")
        else:
            ok("backup_freshness")
        manifest_path = Path(str(latest) + ".manifest.json")
        if not manifest_path.exists():
            fail("backup_manifest", "missing manifest")
        else:
            try:
                manifest = json.loads(manifest_path.read_text())
                archive_sha = hashlib.sha256(latest.read_bytes()).hexdigest()
                if manifest.get("archive_sha256") != archive_sha:
                    fail("backup_manifest", "archive sha256 mismatch")
                else:
                    ok("backup_manifest")
                backup["backup_id"] = manifest.get("backup_id")
                backup["class_a_components"] = manifest.get("class_a_components", [])
            except Exception as e:
                fail("backup_manifest", f"{type(e).__name__}: {e}")

    # Class A coverage from latest manifest
    coverage = {
        "root_vault": False,
        "core_md": False,
        "companion_vault": False,
        "expert_memory": False,
        "pending_memory": False,
        "memory_scripts": False,
    }
    if latest and Path(str(latest) + ".manifest.json").exists():
        try:
            paths = {i.get("path", "") for i in json.loads(Path(str(latest) + ".manifest.json").read_text()).get("files", [])}
            coverage["root_vault"] = any(p.endswith("memory-vault/vault.sqlite3") for p in paths)
            coverage["core_md"] = any(p.endswith("memories/MEMORY.md") for p in paths) and any(p.endswith("memories/USER.md") for p in paths)
            coverage["companion_vault"] = any("profiles/companion/memory-vault/vault.sqlite3" in p for p in paths)
            coverage["expert_memory"] = any(p.startswith("expert-memory/") or "/expert-memory/" in p for p in paths)
            coverage["pending_memory"] = any("pending/memory/" in p for p in paths)
            coverage["memory_scripts"] = any(p.endswith("scripts/memory_vault.py") for p in paths)
            missing = [k for k, v in coverage.items() if not v]
            if missing:
                fail("class_a_backup_coverage", f"missing {missing}", coverage=coverage)
            else:
                ok("class_a_backup_coverage")
        except Exception as e:
            fail("class_a_backup_coverage", f"{type(e).__name__}: {e}")

    # offsite freshness if configured
    offsite_dir = Path(os.environ.get("HERMES_MEMORY_OFFSITE_DIR", "") or (home / "offsite" / "memory-vault"))
    offsite = {"dir": str(offsite_dir), "configured": offsite_dir.exists()}
    if offsite_dir.exists():
        gens = sorted(offsite_dir.glob("vault-*.tar.gz"))
        offsite["generations"] = [g.name for g in gens[-3:]]
        if not gens:
            fail("offsite_freshness", "offsite dir empty")
        else:
            age_d = (datetime.now().astimezone() - datetime.fromtimestamp(gens[-1].stat().st_mtime).astimezone()).days
            offsite["age_days"] = age_d
            if age_d > 14:
                fail("offsite_freshness", f"latest offsite age {age_d}d")
            else:
                ok("offsite_freshness")
    else:
        fail("offsite_freshness", "no offsite destination configured")

    return {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "healthy": not findings,
        "findings": findings,
        "ok_flags": ok_flags,
        "counts": {
            "records": record_n,
            "active": active_n,
            "evidence": evidence_n,
            "vectors": len(vector_ids),
        },
        "index_health": idx_health,
        "db_health": health,
        "backup": backup,
        "class_a_coverage": coverage,
        "offsite": offsite,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_checks()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        if report["healthy"]:
            print("memory_integrity_check: OK")
        else:
            print("memory_integrity_check: ALERT")
            for f in report["findings"]:
                print(f"  - {f['code']}: {f['detail']}")
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
