#!/usr/bin/env python3
"""Losslessly stage legacy mem0-only records into the authoritative vault."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
AGENT_DIR = HERMES_HOME / "hermes-agent"


def load_vault() -> Any:
    path = HERMES_HOME / "scripts" / "memory_vault.py"
    spec = importlib.util.spec_from_file_location("_vault_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def legacy_rows() -> list[dict[str, Any]]:
    sys.path.insert(0, str(AGENT_DIR))
    from plugins.memory.mem0 import Mem0MemoryProvider

    provider = Mem0MemoryProvider()
    provider.initialize("vault-legacy-migration", platform="migration", user_id="max")
    try:
        memory = getattr(getattr(provider, "_backend", None), "_memory", None)
        if memory is None:
            raise RuntimeError(f"mem0 unavailable: {getattr(provider, '_init_error', 'unknown error')}")
        response = memory.get_all(filters=provider._read_filters(), top_k=1000)
        rows = response.get("results", []) if isinstance(response, dict) else response
        if not isinstance(rows, list):
            raise RuntimeError("mem0 get_all returned an unexpected payload")
        return [row for row in rows if "memory-vault:" not in str(row.get("memory", ""))]
    finally:
        provider.shutdown()


def stage(vault: Any, rows: list[dict[str, Any]], *, apply: bool) -> dict[str, Any]:
    vault.ensure_layout()
    records = vault.read_jsonl(vault.RECORDS_PATH)
    known = {str(record.get("legacy_mem0_id", "")) for record in records}
    result: dict[str, Any] = {"found": len(rows), "staged": [], "already_staged": 0, "apply": apply}
    for row in rows:
        legacy_id = str(row.get("id", "")).strip()
        body = str(row.get("memory", "")).strip()
        if not legacy_id or not body:
            continue
        if legacy_id in known:
            result["already_staged"] += 1
            continue
        record = {
            "id": vault.stable_id("legacy-mem0", legacy_id),
            "schema_version": vault.SCHEMA_VERSION,
            "status": "pending",
            "review_status": "needs_user_review",
            "governance_action": "import",
            "risk": "medium",
            "matched_id": "",
            "confidence": 0.0,
            "decision_reason": "legacy mem0-only record retained for explicit vault review",
            "topic": vault.classify(body, ""),
            "title": vault.title_for(body),
            "summary": vault.normalize(body)[:240],
            "body": body,
            "tags": ["candidate", "legacy-mem0"],
            "source": {"kind": "migration", "origin": "mem0"},
            "legacy_mem0_id": legacy_id,
            "content_hash": vault.content_hash(body),
            "created_at": vault.now_iso(),
            "updated_at": vault.now_iso(),
            "core_policy": vault.core_policy(vault.classify(body, ""), body),
        }
        result["staged"].append({"id": record["id"], "legacy_mem0_id": legacy_id, "title": record["title"]})
        if apply:
            _, created = vault.import_pending_record(
                record,
                reason="preserved legacy mem0-only record; pending user review",
                source="memory_vault_migrate_mem0.py",
            )
            if created:
                known.add(legacy_id)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage unmarked mem0 records as vault candidates.")
    parser.add_argument("--apply", action="store_true", help="Write pending imports; omit for a read-only preview.")
    args = parser.parse_args()
    result = stage(load_vault(), legacy_rows(), apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
