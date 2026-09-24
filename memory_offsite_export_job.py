#!/usr/bin/env python3
"""Daily/weekly offsite export to the configured destination (iCloud Drive).

Reads HERMES_MEMORY_OFFSITE_DIR / config.yaml memory.offsite_dir / .env.
Atomic publish via memory_vault_backup.export_offsite.
Silent on success; non-zero + JSON error on failure (Cron delivery alerts).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def load_backup():
    path = Path.home() / ".hermes" / "scripts" / "memory_vault_backup.py"
    spec = importlib.util.spec_from_file_location("_vault_backup_offsite", path)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    return backup


def main() -> int:
    backup = load_backup()
    dest = backup.resolve_offsite_dir()
    try:
        result = backup.export_offsite(dest)
        result["destination"] = str(dest)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        sync = result.get("sync") or {}
        if sync.get("sync_error") or sync.get("upload_pending"):
            # Export succeeded locally; remote still pending is an alert condition.
            print(json.dumps({"offsite_upload_pending": sync}, ensure_ascii=False), file=sys.stderr)
            return 2
        return 0
    except Exception as exc:
        print(json.dumps({"offsite_export_failed": f"{type(exc).__name__}: {exc}", "destination": str(dest)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
