#!/usr/bin/env python3
"""Silent daily local vault snapshot plus archive-integrity verification."""

import importlib.util
import json
from pathlib import Path


path = Path.home() / ".hermes" / "scripts" / "memory_vault_backup.py"
spec = importlib.util.spec_from_file_location("_vault_backup_job", path)
if spec is None or spec.loader is None:
    raise SystemExit(f"Cannot load {path}")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
try:
    snapshot = backup.create_snapshot()
    backup.verify_snapshot(Path(snapshot["archive"]))
except Exception as exc:
    print(json.dumps({"backup_failed": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
    raise SystemExit(1)
