#!/usr/bin/env python3
"""Silent monthly verification that the newest local vault snapshot restores cleanly."""

import importlib.util
import json
from pathlib import Path


path = Path.home() / ".hermes" / "scripts" / "memory_vault_backup.py"
spec = importlib.util.spec_from_file_location("_vault_restore_verify", path)
if spec is None or spec.loader is None:
    raise SystemExit(f"Cannot load {path}")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
try:
    pairs = backup.snapshot_pairs()
    if not pairs:
        raise RuntimeError("no vault snapshot exists")
    backup.verify_snapshot(pairs[-1][0])
except Exception as exc:
    print(json.dumps({"restore_verification_failed": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
    raise SystemExit(1)
