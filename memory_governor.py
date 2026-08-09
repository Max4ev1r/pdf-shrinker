#!/usr/bin/env python3
"""Govern pending Hermes memory candidates with audit-first promotion."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
SCRIPTS_DIR = HERMES_HOME / "scripts"
HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR", str(HERMES_HOME / "hermes-agent"))
).expanduser()
HERMES_PYTHON = HERMES_AGENT_DIR / ".venv" / "bin" / "python"
REPORT_DIR = HERMES_HOME / "reports" / "memory-governance"
STATE_FILE = REPORT_DIR / "state.json"


def ensure_hermes_runtime() -> None:
    if os.environ.get("MEMORY_GOVERNOR_NO_REEXEC"):
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
    env["MEMORY_GOVERNOR_NO_REEXEC"] = "1"
    os.execve(str(HERMES_PYTHON), [str(HERMES_PYTHON), __file__, *sys.argv[1:]], env)


def load_memory_vault():
    module_path = SCRIPTS_DIR / "memory_vault.py"
    spec = importlib.util.spec_from_file_location("memory_vault", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


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
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def candidate_is_auto_promotable(record: dict[str, Any]) -> bool:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    if source.get("origin") == "controlled_learning":
        # Controlled learning no longer owns user-facing durable memory.  Do
        # not let an already-staged legacy candidate become active later.
        return False
    return (
        record.get("status") == "pending"
        and record.get("risk") == "low"
        and record.get("governance_action") == "add"
        and record.get("review_status") == "pending"
        and not record.get("matched_id")
    )


def run_governance(*, dry_run: bool = False, no_sync: bool = False) -> dict[str, Any]:
    vault = load_memory_vault()
    vault.ensure_layout()
    records = vault.read_jsonl(vault.RECORDS_PATH)
    pending = [r for r in records if r.get("status") == "pending"]
    state = read_json(STATE_FILE, {"candidates": {}})
    candidate_state = state.setdefault("candidates", {})

    payload: dict[str, Any] = {
        "generated_at": now_iso(),
        "dry_run": dry_run,
        "no_sync": no_sync,
        "pending_count": len(pending),
        "auto_promoted": [],
        "awaiting_second_pass": [],
        "needs_user_review": [],
        "merge_candidates": [],
        "rejected_stale_state": [],
        "duplicate_candidates": vault.duplicate_candidates(records),
        "sync": None,
        "errors": [],
    }

    active_pending_ids = {r["id"] for r in pending}
    for candidate_id in list(candidate_state):
        if candidate_id not in active_pending_ids:
            payload["rejected_stale_state"].append(candidate_id)
            candidate_state.pop(candidate_id, None)

    promoted_any = False
    for record in pending:
        record_id = record["id"]
        effective_high_risk = vault.is_high_risk_memory(
            str(record.get("body", "")),
            str(record.get("topic", "other")),
        )
        if record.get("governance_action") == "merge" or record.get("matched_id"):
            payload["merge_candidates"].append({
                "id": record_id,
                "matched_id": record.get("matched_id", ""),
                "title": record.get("title", ""),
                "risk": "high" if effective_high_risk else record.get("risk", ""),
                "reason": record.get("decision_reason", ""),
            })
            continue
        if (
            effective_high_risk
            or record.get("review_status") == "needs_user_review"
            or record.get("risk") == "high"
        ):
            if (
                effective_high_risk
                and record.get("risk") != "high"
                and not dry_run
            ):
                try:
                    record = vault.reclassify_pending_risk(
                        record_id,
                        reason=(
                            "current policy classifies this memory as "
                            "sensitive"
                        ),
                    )
                except SystemExit as exc:
                    payload["errors"].append({
                        "id": record_id,
                        "stage": "risk_reclassification",
                        "error": str(exc),
                    })
                    continue
            payload["needs_user_review"].append({
                "id": record_id,
                "title": record.get("title", ""),
                "topic": record.get("topic", ""),
                "risk": "high",
                "reason": (
                    "current policy classifies this memory as sensitive"
                    if effective_high_risk and record.get("risk") != "high"
                    else record.get("decision_reason", "")
                ),
            })
            continue
        if not candidate_is_auto_promotable(record):
            source = record.get("source") if isinstance(record.get("source"), dict) else {}
            payload["needs_user_review"].append({
                "id": record_id,
                "title": record.get("title", ""),
                "topic": record.get("topic", ""),
                "risk": record.get("risk", ""),
                "reason": (
                    "controlled-learning candidates require an authoritative live turn"
                    if source.get("origin") == "controlled_learning"
                    else "candidate does not satisfy low-risk auto-promotion rules"
                ),
            })
            continue

        evidence_count = vault.evidence_count(record_id)
        if evidence_count >= 2:
            if dry_run:
                payload["auto_promoted"].append({
                    "id": record_id, "title": record.get("title", ""),
                    "dry_run": True, "evidence_count": evidence_count,
                })
                continue
            try:
                promoted = vault.promote_record(record_id, reason="memory governor independent-evidence promotion")
                payload["auto_promoted"].append({
                    "id": record_id, "title": promoted.get("title", ""),
                    "evidence_count": evidence_count,
                })
                candidate_state.pop(record_id, None)
                promoted_any = True
            except SystemExit as exc:
                payload["errors"].append({"id": record_id, "stage": "promote", "error": str(exc)})
        else:
            payload["awaiting_second_pass"].append({
                "id": record_id,
                "title": record.get("title", ""),
                "evidence_count": evidence_count,
                "required_evidence": 2,
            })

    state["updated_at"] = now_iso()
    if not dry_run:
        write_json(STATE_FILE, state)
    if promoted_any and not dry_run and not no_sync:
        sync = vault.sync_index_outbox()
        payload["sync"] = sync
        if sync.get("errors"):
            payload["errors"].extend(sync["errors"])
    return payload


def write_report(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = stamp()
    json_path = REPORT_DIR / f"{name}.json"
    md_path = REPORT_DIR / f"{name}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        f"# Hermes Memory Governance - {name}",
        "",
        f"- pending_count: `{payload['pending_count']}`",
        f"- auto_promoted: `{len(payload['auto_promoted'])}`",
        f"- awaiting_second_pass: `{len(payload['awaiting_second_pass'])}`",
        f"- needs_user_review: `{len(payload['needs_user_review'])}`",
        f"- merge_candidates: `{len(payload['merge_candidates'])}`",
        f"- duplicate_candidates: `{len(payload['duplicate_candidates'])}`",
        f"- errors: `{len(payload['errors'])}`",
        "",
    ]
    for section in ("auto_promoted", "awaiting_second_pass", "needs_user_review", "merge_candidates"):
        lines.extend([f"## {section}", ""])
        rows = payload.get(section, [])
        if not rows:
            lines.append("- None.")
        else:
            for row in rows:
                lines.append("- " + json.dumps(row, ensure_ascii=False, sort_keys=True))
        lines.append("")
    if payload.get("errors"):
        lines.extend(["## Errors", ""])
        for error in payload["errors"]:
            lines.append("- " + json.dumps(error, ensure_ascii=False, sort_keys=True))
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    payload["report_json"] = str(json_path)
    payload["report_md"] = str(md_path)


def main() -> int:
    ensure_hermes_runtime()
    parser = argparse.ArgumentParser(description="Govern pending Hermes memory candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Do not promote or sync.")
    parser.add_argument("--no-sync", action="store_true", help="Do not sync promoted records to mem0.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload.")
    args = parser.parse_args()
    payload = run_governance(dry_run=args.dry_run, no_sync=args.no_sync)
    write_report(payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Memory governance written: {payload.get('report_md')}")
    return 1 if payload.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
