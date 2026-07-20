#!/usr/bin/env python3
"""Silent hourly replay of the vault-to-index outbox."""

import importlib.util
import json
from pathlib import Path


path = Path.home() / ".hermes" / "scripts" / "memory_vault.py"
spec = importlib.util.spec_from_file_location("_vault_index_job", path)
if spec is None or spec.loader is None:
    raise SystemExit(f"Cannot load {path}")
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)
result = vault.sync_index_outbox()
if result.get("errors"):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(1)
