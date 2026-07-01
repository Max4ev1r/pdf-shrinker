#!/usr/bin/env python3
"""Apply narrowly scoped, reversible Hermes learning updates.

Phase 3b policy:
- consume the latest filtered learning-action report
- auto-apply only repeated, low-risk, direct-user preference facts
- use Hermes' native MemoryStore for locking, scanning, and capacity checks
- back up and verify every write; restore the backup if verification fails
- queue ambiguous or high-risk candidates without modifying memory

This script never writes MEMORY.md, skills, config, or an external memory
provider. Its only writable knowledge target is memories/USER.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
ACTION_DIR = HERMES_HOME / "reports" / "learning-actions"
REPORT_DIR = HERMES_HOME / "reports" / "controlled-learning"
STATE_FILE = REPORT_DIR / "state.json"
BACKUP_DIR = HERMES_HOME / "backups" / "controlled-learning"
USER_FILE = HERMES_HOME / "memories" / "USER.md"
AGENT_ROOT = HERMES_HOME / "hermes-agent"
HERMES_PYTHON = AGENT_ROOT / "venv" / "bin" / "python"

DIRECT_USER_SOURCES = {
    "weixin",
    "telegram",
    "discord",
    "signal",
    "whatsapp",
    "slack",
    "matrix",
    "mattermost",
    "cli",
    "gateway",
    "web",
}

AUTO_MIN_SUPPORT = 2
QUEUE_KEEP_DAYS = 30


@dataclass
class Fact:
    key: str
    content: str
    support: int
    candidate_ids: list[str]
    source_messages: list[str]


@dataclass
class Decision:
    candidate_id: str
    outcome: str
    reason: str
    fact: str = ""


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def stamp() -> str:
    return now().strftime("%Y%m%d-%H%M%S")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def latest_action_report() -> Path | None:
    files = sorted(ACTION_DIR.glob("*.json"))
    return files[-1] if files else None


def compact(text: str) -> str:
    return re.sub(r"[\s，。；、,.!?！？:：()（）]+", "", text or "").lower()


def safe_item(raw: str) -> str:
    item = re.sub(r"^(?:吃|喝)", "", raw.strip())
    item = re.sub(r"(?:最喜欢|也喜欢|还可以|可以)$", "", item)
    return item[:16]


def extract_preference_facts(text: str) -> list[tuple[str, str]]:
    """Extract only simple food dislikes; broader preferences remain queued."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    found: dict[str, str] = {}
    patterns = [
        r"(?:我)?不喜欢吃(?P<item>[\u4e00-\u9fffA-Za-z0-9·\-]{1,16}?)(?=最喜欢|也喜欢|还可以|$|[，。；、\s])",
        r"(?<!喜)不吃(?P<item>[\u4e00-\u9fffA-Za-z0-9·\-]{1,16}?)(?=最喜欢|也喜欢|还可以|$|[，。；、\s])",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, clean):
            item = safe_item(match.group("item"))
            if not item:
                continue
            key = f"food_dislike:{compact(item)}"
            found[key] = f"饮食偏好：不喜欢吃{item}。"
    return sorted(found.items())


def existing_fact(user_text: str, fact: Fact) -> bool:
    if fact.key.startswith("food_dislike:"):
        item = fact.key.split(":", 1)[1]
        existing = compact(user_text)
        return f"不喜欢吃{item}" in existing or f"不吃{item}" in existing
    return compact(fact.content) in compact(user_text)


def memory_limits() -> tuple[int, int]:
    config = yaml.safe_load((HERMES_HOME / "config.yaml").read_text(encoding="utf-8")) or {}
    memory = config.get("memory") or {}
    return int(memory.get("memory_char_limit", 2200)), int(memory.get("user_char_limit", 1375))


def load_memory_store():
    sys.path.insert(0, str(AGENT_ROOT))
    from tools.memory_tool import MemoryStore

    memory_limit, user_limit = memory_limits()
    store = MemoryStore(memory_char_limit=memory_limit, user_char_limit=user_limit)
    store.load_from_disk()
    return store


def backup_user_file() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"USER-{stamp()}.md.bak"
    if USER_FILE.exists():
        shutil.copy2(USER_FILE, backup)
    else:
        backup.write_text("", encoding="utf-8")
    return backup


def restore_user_file(backup: Path) -> None:
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USER_FILE.with_suffix(".md.controlled-rollback.tmp")
    shutil.copy2(backup, tmp)
    os.replace(tmp, USER_FILE)


def apply_fact(fact: Fact, *, dry_run: bool) -> tuple[str, str]:
    current = USER_FILE.read_text(encoding="utf-8", errors="replace") if USER_FILE.exists() else ""
    if existing_fact(current, fact):
        return "already_present", "equivalent fact already exists in USER.md"
    if dry_run:
        return "would_apply", "dry-run: fact passed all gates"

    backup = backup_user_file()
    try:
        store = load_memory_store()
        result = store.apply_batch("user", [{"action": "add", "content": fact.content}])
        if not result.get("success"):
            return "queued_capacity_or_guard", str(result.get("error") or "MemoryStore rejected write")

        verified = USER_FILE.read_text(encoding="utf-8", errors="replace")
        if not existing_fact(verified, fact):
            restore_user_file(backup)
            return "rolled_back", "write returned success but verification failed; backup restored"
        return "applied", f"verified native MemoryStore write; backup={backup}"
    except Exception as exc:
        restore_user_file(backup)
        return "rolled_back", f"{type(exc).__name__}: {exc}; backup restored"


def build_facts(candidates: list[dict[str, Any]]) -> tuple[list[Fact], list[Decision]]:
    support: dict[str, set[str]] = defaultdict(set)
    messages: dict[str, list[str]] = defaultdict(list)
    content_by_key: dict[str, str] = {}
    decisions: list[Decision] = []

    for candidate in candidates:
        cid = str(candidate.get("id") or "")
        if candidate.get("status") != "stage_user_memory":
            if candidate.get("status") in {"manual_review", "stage_skill_rule"}:
                decisions.append(
                    Decision(cid, "queued_policy", "only low-risk USER.md preference candidates are auto-eligible")
                )
            continue
        if candidate.get("risk") != "low" or int(candidate.get("score") or 0) < 8:
            decisions.append(Decision(cid, "queued_policy", "candidate did not pass low-risk score gate"))
            continue
        source = str(candidate.get("source") or "").lower()
        if source not in DIRECT_USER_SOURCES:
            decisions.append(Decision(cid, "queued_source", f"source {source or 'unknown'} is not a direct-user channel"))
            continue

        extracted = extract_preference_facts(str(candidate.get("text") or ""))
        if not extracted:
            decisions.append(
                Decision(cid, "queued_ambiguous", "candidate could not be reduced to a conservative structured fact")
            )
            continue
        for key, content in extracted:
            support[key].add(cid)
            content_by_key[key] = content
            messages[key].append(str(candidate.get("text") or "")[:240])

    facts = [
        Fact(
            key=key,
            content=content_by_key[key],
            support=len(candidate_ids),
            candidate_ids=sorted(candidate_ids),
            source_messages=messages[key],
        )
        for key, candidate_ids in support.items()
    ]
    facts.sort(key=lambda item: item.key)
    return facts, decisions


def prune_queue(queue: dict[str, Any]) -> dict[str, Any]:
    cutoff = now() - dt.timedelta(days=QUEUE_KEEP_DAYS)
    kept: dict[str, Any] = {}
    for key, value in queue.items():
        try:
            seen = dt.datetime.fromisoformat(str(value.get("last_seen")))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=now().tzinfo)
            if seen >= cutoff:
                kept[key] = value
        except Exception:
            kept[key] = value
    return kept


def decision_queue_key(decision: Decision) -> str:
    raw = f"{decision.candidate_id}|{decision.outcome}|{decision.fact}|{decision.reason}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def write_report(
    action_report: Path,
    facts: list[Fact],
    decisions: list[Decision],
    state: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    base = stamp()
    md_path = REPORT_DIR / f"{base}.md"
    json_path = REPORT_DIR / f"{base}.json"
    outcomes = defaultdict(int)
    for decision in decisions:
        outcomes[decision.outcome] += 1

    lines = [
        f"# Hermes Controlled Learning - {now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "## Guardrails",
        "",
        "- Target: memories/USER.md only.",
        "- Auto-eligible: repeated, low-risk, direct-user, structured preferences.",
        "- High-risk, ambiguous, skill, MEMORY.md, config, and external-memory writes: blocked.",
        "- Write path: native MemoryStore with backup, verification, and rollback.",
        f"- Dry run: {dry_run}",
        "",
        "## Summary",
        "",
        f"- Source action report: {action_report}",
        f"- Structured facts: {len(facts)}",
        f"- Applied: {outcomes['applied']}",
        f"- Already present: {outcomes['already_present']}",
        f"- Queued or blocked: {sum(value for key, value in outcomes.items() if key not in {'applied', 'already_present'})}",
        "",
        "## Decisions",
        "",
    ]
    if not decisions:
        lines.append("- None.")
    for decision in decisions:
        fact = f" fact={decision.fact}" if decision.fact else ""
        lines.append(f"- `{decision.outcome}` candidate={decision.candidate_id or '-'}; {decision.reason}{fact}")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(
        json_path,
        {
            "generated_at": now().isoformat(),
            "dry_run": dry_run,
            "action_report": str(action_report),
            "facts": [asdict(fact) for fact in facts],
            "decisions": [asdict(decision) for decision in decisions],
            "state": state,
        },
    )
    return md_path, json_path


def main() -> int:
    if sys.version_info < (3, 10) and HERMES_PYTHON.exists() and os.environ.get("HERMES_CONTROLLED_REEXEC") != "1":
        env = dict(os.environ)
        env["HERMES_CONTROLLED_REEXEC"] = "1"
        os.execve(str(HERMES_PYTHON), [str(HERMES_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], env)

    parser = argparse.ArgumentParser(description="Run Phase 3b controlled Hermes learning.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-clean", action="store_true")
    args = parser.parse_args()

    action_report = latest_action_report()
    if not action_report:
        raise SystemExit(f"No learning-action report found in {ACTION_DIR}")
    payload = read_json(action_report, {})
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        raise SystemExit(f"Invalid learning-action report: {action_report}")

    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("applied", {})
    old_queue = prune_queue(state.get("queue") if isinstance(state.get("queue"), dict) else {})
    state["queue"] = {
        key: value
        for key, value in old_queue.items()
        if isinstance(value, dict) and value.get("key_version") == 2
    }

    facts, decisions = build_facts(candidates)
    current_user_text = USER_FILE.read_text(encoding="utf-8", errors="replace") if USER_FILE.exists() else ""
    for fact in facts:
        if existing_fact(current_user_text, fact):
            decision = Decision(
                ",".join(fact.candidate_ids),
                "already_present",
                "equivalent fact already exists in USER.md",
                fact.content,
            )
        elif fact.support < AUTO_MIN_SUPPORT:
            decision = Decision(
                fact.candidate_ids[0] if fact.candidate_ids else "",
                "queued_insufficient_support",
                f"support={fact.support}, requires {AUTO_MIN_SUPPORT} distinct candidates",
                fact.content,
            )
        else:
            outcome, reason = apply_fact(fact, dry_run=args.dry_run)
            decision = Decision(
                ",".join(fact.candidate_ids),
                outcome,
                reason,
                fact.content,
            )
            if outcome == "applied":
                state["applied"][fact.key] = {
                    "content": fact.content,
                    "applied_at": now().isoformat(),
                    "support": fact.support,
                    "candidate_ids": fact.candidate_ids,
                }
        decisions.append(decision)

    for decision in decisions:
        queue_key = decision_queue_key(decision)
        if decision.outcome.startswith("queued"):
            state["queue"][queue_key] = {
                "key_version": 2,
                "candidate_id": decision.candidate_id,
                "outcome": decision.outcome,
                "reason": decision.reason,
                "fact": decision.fact,
                "last_seen": now().isoformat(),
            }
        else:
            state["queue"].pop(queue_key, None)
    state["last_action_report"] = str(action_report)
    state["updated_at"] = now().isoformat()
    if not args.dry_run:
        write_json(STATE_FILE, state)
    md_path, json_path = write_report(action_report, facts, decisions, state, dry_run=args.dry_run)

    applied = [decision for decision in decisions if decision.outcome in {"applied", "rolled_back"}]
    if applied or args.print_clean:
        print(f"Hermes controlled learning: {len(applied)} change events. Report: {md_path}; JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
