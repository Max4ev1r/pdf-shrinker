#!/usr/bin/env python3
"""Generate a non-invasive learning review from Hermes session history.

The report identifies candidate lessons and failure patterns. It does not
modify MEMORY.md, USER.md, skills, cron jobs, or config.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
STATE_DB = HERMES_HOME / "state.db"
REPORT_DIR = HERMES_HOME / "reports" / "learning-review"

CORRECTION_PATTERNS = [
    "不对",
    "不是",
    "错了",
    "错误",
    "纠正",
    "你上次",
    "实际没",
    "反复",
    "变卦",
    "不要",
    "别",
    "不应该",
    "我说的是",
    "我指的是",
]

PREFERENCE_PATTERNS = [
    "我喜欢",
    "我不喜欢",
    "我偏好",
    "以后",
    "记住",
    "默认",
    "不要",
    "不用",
    "必须",
    "优先",
    "我要求",
    "明确要求",
]

STRONG_PREFERENCE_PATTERNS = [
    "我喜欢",
    "我不喜欢",
    "我偏好",
    "记住",
    "默认",
    "必须",
    "优先",
    "我要求",
    "明确要求",
]

FAILURE_PATTERNS = [
    "未检索到",
    "无法",
    "失败",
    "403 Forbidden",
    "Connection reset",
    "timeout",
    "No relevant",
    "no content",
    "报错",
]

HIGH_RISK_TERMS = [
    "医疗",
    "医生",
    "用药",
    "血压",
    "补剂",
    "宝宝",
    "法律",
    "合同",
    "财税",
    "税",
    "社保",
]


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def since_timestamp(days: int) -> float:
    return (now() - dt.timedelta(days=days)).timestamp()


def snippet(text: str, limit: int = 220) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def has_any(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern.lower() in (text or "").lower() for pattern in patterns)


def fetch_messages(days: int, include_cron: bool = False) -> list[dict]:
    source_filter = "" if include_cron else "and coalesce(s.source, '') != 'cron'"
    with sqlite3.connect(STATE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select m.session_id, m.role, m.content, m.tool_name, m.timestamp,
                   s.source, s.title
            from messages m
            left join sessions s on s.id = m.session_id
            where m.timestamp >= ?
              and coalesce(m.active, 1) = 1
              and m.content is not null
              {source_filter}
            order by m.timestamp asc
            """,
            (since_timestamp(days),),
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_sessions(days: int, include_cron: bool = False) -> list[dict]:
    source_filter = "" if include_cron else "and coalesce(source, '') != 'cron'"
    with sqlite3.connect(STATE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select id, source, title, message_count, tool_call_count, api_call_count,
                   started_at, estimated_cost_usd
            from sessions
            where started_at >= ?
              {source_filter}
            order by started_at desc
            """,
            (since_timestamp(days),),
        ).fetchall()
    return [dict(row) for row in rows]


def find_candidates(messages: list[dict], patterns: list[str], roles: set[str], limit: int) -> list[dict]:
    found = []
    for row in messages:
        if row["role"] not in roles:
            continue
        content = row.get("content") or ""
        if row["role"] == "tool" and content.lstrip().startswith("<untrusted_tool_result"):
            # Most tool payloads include generic safety wrappers. Do not treat
            # the wrapper itself as a failure signal.
            body = content.split(">", 1)[-1]
            if not has_any(body, ["403 Forbidden", "Connection reset", "timeout", "failed", "失败", "报错"]):
                continue
        if row["role"] == "user" and "要不要" in content and not has_any(content, STRONG_PREFERENCE_PATTERNS):
            continue
        if has_any(content, patterns):
            found.append(row)
    return found[-limit:]


def report(days: int, limit: int, include_cron: bool = False) -> tuple[Path, str]:
    messages = fetch_messages(days, include_cron=include_cron)
    sessions = fetch_sessions(days, include_cron=include_cron)
    by_role = Counter(row["role"] for row in messages)
    by_source = Counter(row.get("source") or "unknown" for row in messages)
    corrections = find_candidates(messages, CORRECTION_PATTERNS, {"user"}, limit)
    preferences = find_candidates(messages, PREFERENCE_PATTERNS, {"user"}, limit)
    failures = find_candidates(messages, FAILURE_PATTERNS, {"assistant", "tool"}, limit)

    high_risk = [row for row in corrections + preferences if has_any(row.get("content") or "", HIGH_RISK_TERMS)]
    session_hits: dict[str, int] = defaultdict(int)
    for row in corrections + preferences + failures:
        session_hits[row["session_id"]] += 1
    top_sessions = sorted(session_hits.items(), key=lambda item: item[1], reverse=True)[:10]

    lines = [
        f"# Hermes Learning Review - {now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Window: last {days} days",
        "",
        "## Summary",
        "",
        f"- Messages scanned: {len(messages)}",
        f"- Sessions scanned: {len(sessions)}",
        f"- Roles: {dict(by_role)}",
        f"- Sources: {dict(by_source)}",
        f"- Cron included: {include_cron}",
        f"- Correction candidates: {len(corrections)}",
        f"- Preference/rule candidates: {len(preferences)}",
        f"- Failure candidates: {len(failures)}",
        f"- High-risk candidates requiring manual review: {len(high_risk)}",
        "",
        "## Candidate Memory Updates",
        "",
        "These are candidates only. Review before writing USER.md or MEMORY.md.",
        "",
    ]
    if not preferences:
        lines.append("- None found.")
    for row in preferences:
        ts = dt.datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
        text = snippet(row["content"])
        lines.append(f"- {ts} `{row['session_id']}` {text}")

    lines.extend(["", "## Candidate Corrections / Old Mistakes", ""])
    if not corrections:
        lines.append("- None found.")
    for row in corrections:
        ts = dt.datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
        text = snippet(row["content"])
        lines.append(f"- {ts} `{row['session_id']}` {text}")

    lines.extend(["", "## Tool / Answer Failure Signals", ""])
    if not failures:
        lines.append("- None found.")
    for row in failures:
        ts = dt.datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
        label = row.get("tool_name") or row["role"]
        text = snippet(row["content"])
        lines.append(f"- {ts} `{row['session_id']}` `{label}` {text}")

    lines.extend(["", "## High-Risk Manual Review", ""])
    if not high_risk:
        lines.append("- None found.")
    for row in high_risk[-limit:]:
        ts = dt.datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M")
        text = snippet(row["content"])
        lines.append(f"- {ts} `{row['session_id']}` {text}")

    lines.extend(["", "## Sessions To Inspect First", ""])
    if not top_sessions:
        lines.append("- None found.")
    else:
        title_map = {row["id"]: row.get("title") or "" for row in sessions}
        for session_id, count in top_sessions:
            title = title_map.get(session_id, "")
            lines.append(f"- `{session_id}` hits={count} title={title}")

    lines.extend(
        [
            "",
            "## Recommended Actions",
            "",
            "- Promote only stable, repeated, or high-impact user facts into USER.md.",
            "- Put temporary plans and active reminders into MEMORY.md, not SOUL.md.",
            "- Convert repeated corrections into skill tests before changing broad behavior.",
            "- Do not auto-update medical, legal, finance, or baby-related conclusions.",
            "",
        ]
    )

    wrapped = []
    for line in lines:
        if line.startswith("- ") and len(line) > 140:
            wrapped.append(textwrap.fill(line, width=120, subsequent_indent="  "))
        else:
            wrapped.append(line)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{now().strftime('%Y%m%d-%H%M%S')}.md"
    text = "\n".join(wrapped) + "\n"
    path.write_text(text, encoding="utf-8")
    return path, text


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Hermes learning review report.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--include-cron", action="store_true", help="Include cron sessions in the review.")
    args = parser.parse_args()
    if not STATE_DB.exists():
        raise SystemExit(f"state.db not found: {STATE_DB}")
    path, _ = report(days=max(1, args.days), limit=max(1, args.limit), include_cron=args.include_cron)
    print(f"Learning review written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
