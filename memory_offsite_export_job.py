#!/usr/bin/env python3
"""Weekly provider-agnostic offsite export of one verified vault generation.

Destination is HERMES_MEMORY_OFFSITE_DIR (external disk / NAS mount / synced
folder). No cloud account is created here. Silent when healthy; alerts on fail.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def main() -> int:
    configured = os.environ.get("HERMES_MEMORY_OFFSITE_DIR", "")
    dest = configured or str(Path.home() / ".hermes" / "offsite" / "memory-vault")
    dest_path = Path(dest).expanduser()
    backup_path = Path.home() / ".hermes" / "scripts" / "memory_vault_backup.py"
    spec = importlib.util.spec_from_file_location("_vault_backup_offsite", backup_path)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    try:
        result = backup.export_offsite(dest_path)
        result["destination"] = str(dest_path)
        result["failure_domain_split"] = bool(configured)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"offsite_export_failed": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
