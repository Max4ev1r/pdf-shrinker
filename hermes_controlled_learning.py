#!/usr/bin/env python3
"""Review missed durable user facts without bypassing the live Vault owner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
ACTION_DIR = HERMES_HOME / "reports" / "learning-actions"
REPORT_DIR = HERMES_HOME / "reports" / "controlled-learning"
STATE_FILE = REPORT_DIR / "state.json"
AGENT_ROOT = HERMES_HOME / "hermes-agent"
HERMES_PYTHON = AGENT_ROOT / ".venv" / "bin" / "python"
VAULT_SCRIPT = HERMES_HOME / "scripts" / "memory_vault.py"

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


def extract_durable_facts(text: str) -> list[tuple[str, str]]:
    """Reduce only high-confidence direct-user statements to durable facts."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    found: dict[str, str] = {}

    explicit = re.match(
        r"^(?:(?:请|麻烦|帮我|你)\s*)?(?:长期)?"
        r"(?:记住|记一下|记下来)(?:这件事|这个|以下内容)?"
        r"\s*[：:，,\s]+(.+)$",
        clean,
        flags=re.DOTALL,
    )
    if explicit:
        content = explicit.group(1).strip()
        if 2 <= len(content) <= 1200:
            key = "explicit:" + hashlib.sha1(
                compact(content).encode("utf-8")
            ).hexdigest()[:16]
            found[key] = content

    food_patterns = [
        r"(?:我)?不喜欢吃(?P<item>[\u4e00-\u9fffA-Za-z0-9·\-]{1,16}?)(?=最喜欢|也喜欢|还可以|$|[，。；、\s])",
        r"(?<!喜)不吃(?P<item>[\u4e00-\u9fffA-Za-z0-9·\-]{1,16}?)(?=最喜欢|也喜欢|还可以|$|[，。；、\s])",
    ]
    for pattern in food_patterns:
        for match in re.finditer(pattern, clean):
            item = safe_item(match.group("item"))
            if not item:
                continue
            key = f"food_dislike:{compact(item)}"
            found[key] = f"饮食偏好：不喜欢吃{item}。"

    spouse = re.search(
        r"(?:我)?(?:老婆|妻子|丈夫|老公)(?:叫|是)"
        r"(?P<name>[\u4e00-\u9fff]{2,6})(?=$|[，。；、\s])",
        clean,
    )
    if spouse:
        name = spouse.group("name")
        found[f"family:spouse:{compact(name)}"] = f"配偶：{name}。"

    birthday = re.search(
        r"(?:我的)?生日(?:是|为)?\s*"
        r"(?P<date>\d{4}[-年/]\d{1,2}[-月/]\d{1,2}[日号]?)",
        clean,
    )
    if birthday:
        date = birthday.group("date")
        found[f"birthday:{compact(date)}"] = f"生日：{date}。"

    if re.match(
        r"^(?:我(?:一直|长期)?(?:喜欢|不喜欢|偏好)|"
        r"以后|今后|默认|每次|始终)",
        clean,
    ) and len(clean) <= 240:
        key = "preference:" + hashlib.sha1(
            compact(clean).encode("utf-8")
        ).hexdigest()[:16]
        found[key] = clean

    return sorted(found.items())


def load_memory_vault():
    spec = importlib.util.spec_from_file_location(
        "_hermes_controlled_learning_vault",
        VAULT_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {VAULT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage_fact(fact: Fact, *, dry_run: bool) -> tuple[str, str]:
    # This scheduled background job has no live turn owner, profile routing,
    # or user acknowledgement boundary.  It must not create user-facing Vault
    # records (in particular in the legacy main-home store).  Keep its review
    # report so a user-approved path can act later, but fail closed here.
    return (
        "blocked_non_authoritative",
        "background learning may not write durable Vault memory without a live authoritative turn",
    )


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
                    Decision(cid, "queued_policy", "only low-risk durable user facts are eligible for vault staging")
                )
            continue
        if candidate.get("risk") != "low" or int(candidate.get("score") or 0) < 8:
            decisions.append(Decision(cid, "queued_policy", "candidate did not pass low-risk score gate"))
            continue
        source = str(candidate.get("source") or "").lower()
        if source not in DIRECT_USER_SOURCES:
            decisions.append(Decision(cid, "queued_source", f"source {source or 'unknown'} is not a direct-user channel"))
            continue

        extracted = extract_durable_facts(str(candidate.get("text") or ""))
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
        "- Target: the local Vault authority only.",
        "- Auto-eligible: low-risk, direct-user, conservatively structured facts.",
        "- Every recovered fact is staged through Vault governance; this job never activates it.",
        "- USER.md, MEMORY.md, skills, config, and secrets are never written.",
        f"- Dry run: {dry_run}",
        "",
        "## Summary",
        "",
        f"- Source action report: {action_report}",
        f"- Structured facts: {len(facts)}",
        f"- Staged: {outcomes['staged']}",
        f"- Already stored: {outcomes['already_active'] + outcomes['already_staged']}",
        f"- Queued or blocked: {sum(value for key, value in outcomes.items() if key not in {'staged', 'already_active', 'already_staged'})}",
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
            "target": "vault",
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
    state.setdefault("staged", {})
    old_queue = prune_queue(state.get("queue") if isinstance(state.get("queue"), dict) else {})
    state["queue"] = {
        key: value
        for key, value in old_queue.items()
        if isinstance(value, dict) and value.get("key_version") == 3
    }

    facts, decisions = build_facts(candidates)
    for fact in facts:
        outcome, reason = stage_fact(fact, dry_run=args.dry_run)
        decision = Decision(
            ",".join(fact.candidate_ids),
            outcome,
            reason,
            fact.content,
        )
        if outcome == "staged":
            state["staged"][fact.key] = {
                "content": fact.content,
                "staged_at": now().isoformat(),
                "support": fact.support,
                "candidate_ids": fact.candidate_ids,
            }
        decisions.append(decision)

    for decision in decisions:
        queue_key = decision_queue_key(decision)
        if decision.outcome.startswith("queued"):
            state["queue"][queue_key] = {
                "key_version": 3,
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

    changes = [
        decision
        for decision in decisions
        if decision.outcome in {"staged", "error"}
    ]
    if changes or args.print_clean:
        print(f"Hermes controlled learning: {len(changes)} change events. Report: {md_path}; JSON: {json_path}")
    return 1 if any(decision.outcome == "error" for decision in decisions) else 0


if __name__ == "__main__":
    raise SystemExit(main())
