#!/usr/bin/env python3
"""Build a reviewable action queue from Hermes learning signals.

This is the Phase 3a step for self-improvement:
- read session history and recent shadow reports
- classify learning candidates by risk and target
- suppress quoted history, one-off task requirements, and stale tool errors
- write markdown/json reports for human review

It deliberately does not modify USER.md, MEMORY.md, skills, config, cron jobs,
or any external memory backend.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import textwrap
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
STATE_DB = HERMES_HOME / "state.db"
REPORT_DIR = HERMES_HOME / "reports" / "learning-actions"
MEMORY_SHADOW_DIR = HERMES_HOME / "reports" / "memory-shadow"
FAILURE_MAX_AGE_HOURS = 36

STRONG_CORRECTION_PATTERNS = [
    "说错",
    "错了",
    "我指的是",
    "我说的是",
    "你上次",
    "刚刚不是",
]

PERSONAL_PREFERENCE_PATTERNS = [
    "我喜欢",
    "我不喜欢",
    "我偏好",
    "不太喜欢",
    "不爱吃",
    "不吃",
    "默认",
    "记住",
    "我老婆叫",
    "我老婆是",
    "我丈夫",
    "我老公",
    "我儿子",
    "我女儿",
    "我宝宝",
    "我住在",
    "我的生日",
    "生日是",
    "不要再",
    "以后都",
    "以后给我",
    "每次都",
]

FOOD_PREFERENCE_TERMS = [
    "吃",
    "桃",
    "糖",
    "巧克力",
    "薯片",
    "番茄味",
    "酸",
    "咸味",
    "椰子",
]

PERSONAL_FACT_TERMS = [
    "老婆",
    "妻子",
    "丈夫",
    "老公",
    "儿子",
    "女儿",
    "宝宝",
    "孩子",
    "生日",
    "出生",
    "住在",
    "偏好",
]

PROCESS_RULE_PATTERNS = [
    "必须",
    "优先",
    "要求",
    "不要营销话术",
    "不要官方宣传",
    "不是官方宣传",
    "真实评价",
    "配方角度",
    "成分表",
    "科学依据",
    "官方说明书",
    "FDA",
    "国家药监局",
    "专业指南",
    "完整详细",
]

DURABLE_PROCESS_PATTERNS = [
    "以后",
    "今后",
    "默认",
    "始终",
    "每次",
    "一律",
    "不要再",
    "长期遵循",
]

PROCESS_THEME_PATTERNS = {
    "community_evidence": [
        "不要官方宣传",
        "不是官方宣传",
        "真实评价",
        "玩家社区",
        "用户评价",
    ],
    "formula_evidence": [
        "配方角度",
        "成分表",
        "活性成分",
        "表活体系",
        "不要营销话术",
    ],
    "primary_sources": [
        "科学依据",
        "官方说明书",
        "FDA",
        "国家药监局",
        "专业指南",
    ],
    "market_availability": [
        "京东",
        "天猫",
        "国内有售",
        "中国可买",
    ],
}

PROCESS_THEME_RULES = {
    "community_evidence": "调研产品或游戏时，优先核对真实用户或玩家社区评价，并与官方宣传区分。",
    "formula_evidence": "产品分析优先核对配方、成分及其作用依据，避免复述营销话术。",
    "primary_sources": "高风险事实优先使用官方说明书、监管记录或专业指南等一手来源。",
    "market_availability": "推荐产品时核实目标市场的实际可购买性。",
}

ONE_OFF_TASK_PATTERNS = [
    "搜索",
    "调研",
    "查找",
    "查一下",
    "在京东搜索",
    "设置一个提醒",
    "设置提醒",
]

QUESTION_NOISE_PATTERNS = [
    "是不是",
    "对不对",
    "要不要",
    "能不能",
    "可以吗",
    "有用吗",
]

HIGH_RISK_TERMS = [
    "医疗",
    "医生",
    "医院",
    "用药",
    "药",
    "服用",
    "血压",
    "血糖",
    "补剂",
    "维生素",
    "鱼油",
    "甘氨酸镁",
    "叶酸",
    "护肝片",
    "替尔泊肽",
    "tirzepatide",
    "Mounjaro",
    "注射",
    "剂量",
    "肝",
    "肝脏",
    "肾脏",
    "散利痛",
    "足弓",
    "扁平足",
    "跟腱",
    "疼",
    "宝宝",
    "孕",
    "法律",
    "合同",
    "财税",
    "社保",
    "税",
    "股票",
    "投资",
    "保险",
]

FAILURE_PATTERNS = [
    "HTTP 429",
    "quota exhausted",
    "rate limited",
    "Connection reset",
    "TimeoutError",
    "Traceback",
    "RuntimeError:",
    "API call failed",
    "MCP call failed",
    '"success": false',
    "'success': false",
]

SOFT_WEB_FAILURE_PATTERNS = [
    "403 Forbidden",
    "404 Not Found",
    "Local extraction failed",
    '"error":',
]

STATUS_ORDER = [
    "stage_user_memory",
    "stage_skill_rule",
    "manual_review",
    "investigate_failure",
    "ignored_noise",
]


@dataclass
class Candidate:
    id: str
    status: str
    target: str
    risk: str
    score: int
    kind: str
    reason: str
    timestamp: str
    session_id: str
    source: str
    title: str
    role: str
    tool_name: str
    text: str


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def since_timestamp(days: int) -> float:
    return (now() - dt.timedelta(days=days)).timestamp()


def clean_text(text: str, limit: int = 500) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def has_any(text: str, patterns: Iterable[str]) -> bool:
    lower = (text or "").lower()
    return any(pattern.lower() in lower for pattern in patterns)


def matched_terms(text: str, patterns: Iterable[str]) -> list[str]:
    lower = (text or "").lower()
    return [pattern for pattern in patterns if pattern.lower() in lower]


def candidate_id(row: dict, status: str, text: str) -> str:
    raw = f"{row.get('session_id')}|{row.get('timestamp')}|{status}|{clean_text(text, 300)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def dedupe_key(text: str, status: str) -> str:
    compact = re.sub(r"\s+", "", text or "").lower()
    compact = compact.replace("？", "?")
    return hashlib.sha1(f"{status}|{compact[:600]}".encode("utf-8")).hexdigest()[:12]


def is_untrusted_wrapper(text: str) -> bool:
    return (text or "").lstrip().startswith("<untrusted_tool_result")


def is_context_compaction(text: str) -> bool:
    head = (text or "").lstrip()[:500].upper()
    return head.startswith("[CONTEXT COMPACTION") or (
        "CONTEXT COMPACTION" in head and "REFERENCE ONLY" in head
    )


def is_generated_image_description(text: str) -> bool:
    head = clean_text(text, 120)
    return head.startswith("[The user sent an image") or "Here's what I can see" in head


def is_document_reference(text: str) -> bool:
    clean = clean_text(text, 240)
    references_document = has_any(clean, ["这份报告", "这个报告", "我的身体评估报告", "这个文件", "这份文件"])
    asks_to_remember = has_any(clean, ["记住", "保存", "存下来"])
    return references_document and asks_to_remember and len(clean) < 160


def wrapper_body(text: str) -> str:
    if not is_untrusted_wrapper(text):
        return text or ""
    return (text or "").split(">", 1)[-1]


def is_explicit_correction(text: str) -> bool:
    if "对不对" in text and not has_any(text, STRONG_CORRECTION_PATTERNS):
        return False
    if re.search(r"(^|[，,。；;\s])不对(?!吗|么)", text or ""):
        return True
    if has_any(text, STRONG_CORRECTION_PATTERNS):
        return True
    scrubbed = (text or "").replace("是不是", "").replace("还是不是", "")
    return bool(re.search(r"(^|[，,。；;\s])不是(?!不是)", scrubbed))


def is_question_noise(text: str) -> bool:
    return has_any(text, QUESTION_NOISE_PATTERNS) and not is_explicit_correction(text)


def is_personal_preference(text: str) -> bool:
    if has_any(text, PERSONAL_PREFERENCE_PATTERNS):
        return True
    if "喜欢" in text and has_any(text, FOOD_PREFERENCE_TERMS) and not has_any(text, ONE_OFF_TASK_PATTERNS):
        return True
    return False


def is_personal_fact(text: str) -> bool:
    return (
        has_any(text, [*FOOD_PREFERENCE_TERMS, *PERSONAL_FACT_TERMS])
        or has_any(text, PERSONAL_PREFERENCE_PATTERNS)
        or bool(re.search(
            r"(?:^|[，,。；;\s])我(?:的|家|现在|用|有|是|跟)",
            text or "",
        ))
    )


def is_process_rule(text: str) -> bool:
    return has_any(text, PROCESS_RULE_PATTERNS)


def process_themes(text: str) -> set[str]:
    return {
        theme
        for theme, patterns in PROCESS_THEME_PATTERNS.items()
        if has_any(text, patterns)
    }


def has_durable_process_language(text: str) -> bool:
    return has_any(text, DURABLE_PROCESS_PATTERNS)


def normalized_process_rule(themes: set[str], original: str) -> str:
    rules = [PROCESS_THEME_RULES[theme] for theme in sorted(themes) if theme in PROCESS_THEME_RULES]
    return " ".join(rules) if rules else clean_text(original)


def is_one_off_task(text: str) -> bool:
    if "提醒" in text and ("设置" in text or "七天后" in text):
        return True
    return has_any(text, ONE_OFF_TASK_PATTERNS) and len(text) > 70


def risk_for(text: str) -> tuple[str, list[str]]:
    terms = matched_terms(text, HIGH_RISK_TERMS)
    if terms:
        return "high", terms
    return "low", []


def fetch_messages(days: int, include_cron: bool) -> list[dict]:
    source_filter = "" if include_cron else "and coalesce(s.source, '') != 'cron'"
    with sqlite3.connect(STATE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select m.id, m.session_id, m.role, m.content, m.tool_name, m.timestamp,
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


def fetch_sessions(days: int, include_cron: bool) -> list[dict]:
    source_filter = "" if include_cron else "and coalesce(source, '') != 'cron'"
    with sqlite3.connect(STATE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select id, source, title, started_at, ended_at, message_count,
                   tool_call_count, api_call_count, estimated_cost_usd
            from sessions
            where started_at >= ?
              {source_filter}
            order by started_at desc
            """,
            (since_timestamp(days),),
        ).fetchall()
    return [dict(row) for row in rows]


def make_candidate(
    row: dict,
    *,
    status: str,
    target: str,
    risk: str,
    score: int,
    kind: str,
    reason: str,
    text: str,
) -> Candidate:
    ts = dt.datetime.fromtimestamp(float(row["timestamp"])).astimezone().strftime("%Y-%m-%d %H:%M")
    return Candidate(
        id=candidate_id(row, status, text),
        status=status,
        target=target,
        risk=risk,
        score=score,
        kind=kind,
        reason=reason,
        timestamp=ts,
        session_id=row.get("session_id") or "",
        source=row.get("source") or "unknown",
        title=row.get("title") or "",
        role=row.get("role") or "",
        tool_name=row.get("tool_name") or "",
        text=clean_text(text),
    )


def classify_user_message(row: dict, repeated_process_themes: set[str]) -> Candidate | None:
    text = row.get("content") or ""
    clean = clean_text(text)

    if (row.get("source") or "").lower() == "subagent":
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk="low",
            score=0,
            kind="delegated_prompt",
            reason="subagent task prompts are generated execution context, not direct user learning",
            text=clean,
        )

    if is_context_compaction(text):
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk="low",
            score=0,
            kind="context_compaction",
            reason="context handoff is reference text, not a current user learning item",
            text=clean,
        )

    if is_document_reference(text):
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk="low",
            score=0,
            kind="document_reference",
            reason="document reference lacks the underlying facts needed for durable memory",
            text=clean,
        )

    risk, risk_terms = risk_for(text)
    correction = is_explicit_correction(text)
    preference = is_personal_preference(text)
    process = is_process_rule(text)
    one_off = is_one_off_task(text)
    themes = process_themes(text)
    reusable_process = has_durable_process_language(text) or bool(themes & repeated_process_themes)

    if is_generated_image_description(text):
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk=risk,
            score=0,
            kind="generated_image_description",
            reason="generated image description is context, not a user learning item",
            text=clean,
        )

    if is_question_noise(text) and not preference and not process:
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk=risk,
            score=0,
            kind="question_noise",
            reason="question phrasing without a durable correction",
            text=clean,
        )

    if one_off and not preference and (not process or not reusable_process):
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk=risk,
            score=0,
            kind="one_off_task",
            reason="one-off task requirements lack a repeated or explicitly durable process rule",
            text=clean,
        )

    if preference:
        if risk == "high":
            return make_candidate(
                row,
                status="manual_review",
                target="Vault candidate",
                risk=risk,
                score=5,
                kind="high_risk_preference",
                reason=f"preference intersects high-risk terms: {', '.join(risk_terms[:4])}",
                text=clean,
            )
        return make_candidate(
            row,
            status="stage_user_memory",
            target="Vault candidate",
            risk=risk,
            score=8,
            kind="personal_preference",
            reason="stable user preference or default",
            text=clean,
        )

    if process and reusable_process:
        if risk == "high":
            return make_candidate(
                row,
                status="manual_review",
                target="skill safety rule candidate",
                risk=risk,
                score=6,
                kind="high_risk_process_rule",
                reason=f"process rule intersects high-risk terms: {', '.join(risk_terms[:4])}",
                text=clean,
            )
        return make_candidate(
            row,
            status="stage_skill_rule",
            target="research/product skill candidate",
            risk=risk,
            score=7,
            kind="process_rule",
            reason="reusable answer-quality rule confirmed by durable wording or repetition",
            text=normalized_process_rule(themes & repeated_process_themes, clean),
        )

    if process:
        return make_candidate(
            row,
            status="ignored_noise",
            target="ignore",
            risk=risk,
            score=0,
            kind="unconfirmed_process_rule",
            reason="process wording appeared once without durable language; wait for repetition",
            text=clean,
        )

    if correction:
        if "说错" in text and len(clean) < 40:
            return make_candidate(
                row,
                status="ignored_noise",
                target="ignore",
                risk=risk,
                score=1,
                kind="local_correction",
                reason="too contextual to promote without surrounding transcript",
                text=clean,
            )
        if risk == "high":
            return make_candidate(
                row,
                status="manual_review",
                target="skill or Vault candidate",
                risk=risk,
                score=5,
                kind="high_risk_correction",
                reason=f"correction intersects high-risk terms: {', '.join(risk_terms[:4])}",
                text=clean,
            )
        is_personal = is_personal_fact(text)
        target = "Vault update candidate" if is_personal else "skill rule candidate"
        return make_candidate(
            row,
            status="manual_review" if is_personal else "stage_skill_rule",
            target=target,
            risk=risk,
            score=6,
            kind="personal_correction" if is_personal else "explicit_correction",
            reason=(
                "personal correction requires the current memory and a complete replacement"
                if is_personal
                else "explicit user correction"
            ),
            text=clean,
        )

    return None


def classify_failure(row: dict) -> Candidate | None:
    if row.get("role") not in {"assistant", "tool"}:
        return None
    try:
        age_hours = max(0.0, (now().timestamp() - float(row.get("timestamp") or 0)) / 3600.0)
    except (TypeError, ValueError):
        age_hours = FAILURE_MAX_AGE_HOURS + 1
    if age_hours > FAILURE_MAX_AGE_HOURS:
        return None
    text = row.get("content") or ""
    body = wrapper_body(text)
    tool_name = row.get("tool_name") or ""
    if is_context_compaction(body) or is_untrusted_wrapper(text):
        return None

    payload: object | None = None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        pass

    if isinstance(payload, dict):
        top_success = payload.get("success")
        top_status = str(payload.get("status") or "").lower()
        top_error = payload.get("error")
        if top_success is True and not top_error:
            return None
        if top_status in {"success", "ok", "completed"}:
            if tool_name != "execute_code":
                return None
            body = str(payload.get("output") or "")

    hard_failure = has_any(body, FAILURE_PATTERNS)
    soft_web_failure = has_any(body, SOFT_WEB_FAILURE_PATTERNS)
    if tool_name in {"web_extract", "web_search"} and soft_web_failure and not hard_failure:
        return None
    if not hard_failure:
        return None
    return make_candidate(
        row,
        status="investigate_failure",
        target="operational follow-up",
        risk="operational",
        score=4,
        kind="failure_signal",
        reason="tool or answer contains an explicit failure marker",
        text=clean_text(body),
    )


def repeated_process_themes(messages: list[dict]) -> set[str]:
    sessions_by_theme: dict[str, set[str]] = {}
    messages_by_theme: Counter[str] = Counter()
    for row in messages:
        if row.get("role") != "user":
            continue
        if (row.get("source") or "").lower() == "subagent":
            continue
        text = row.get("content") or ""
        if is_context_compaction(text):
            continue
        for theme in process_themes(text):
            messages_by_theme[theme] += 1
            sessions_by_theme.setdefault(theme, set()).add(str(row.get("session_id") or ""))
    return {
        theme
        for theme, count in messages_by_theme.items()
        if count >= 2 and len(sessions_by_theme.get(theme, set())) >= 1
    }


def classify(messages: list[dict], include_noise: bool) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    repeated_themes = repeated_process_themes(messages)
    for row in messages:
        item = (
            classify_user_message(row, repeated_themes)
            if row.get("role") == "user"
            else classify_failure(row)
        )
        if not item:
            continue
        if item.status == "ignored_noise" and not include_noise:
            continue
        key = dedupe_key(item.text, item.status)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)
    candidates.sort(key=lambda c: (STATUS_ORDER.index(c.status), -c.score, c.timestamp))
    return candidates


def latest_shadow_gate() -> dict:
    files = sorted(MEMORY_SHADOW_DIR.glob("*.md"))
    if not files:
        return {
            "file": "",
            "overall": "missing",
            "action": "hold",
            "reason": "no memory-shadow reports found",
        }
    latest = files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Overall:\s+(\w+)", text, re.MULTILINE)
    overall = match.group(1) if match else "unknown"
    keep_unchanged = "Keep `memory.provider` unchanged" in text
    action = "hold"
    reason = "shadow report recommends keeping production memory unchanged"
    if overall != "PASS":
        reason = f"latest shadow report is {overall}"
    elif not keep_unchanged:
        reason = "shadow health is passing, but semantic recall still needs manual approval"
    return {
        "file": str(latest),
        "overall": overall,
        "action": action,
        "reason": reason,
    }


def group_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts = Counter(item.status for item in candidates)
    return {status: counts.get(status, 0) for status in STATUS_ORDER}


def candidate_line(item: Candidate) -> str:
    label = item.tool_name or item.role
    return (
        f"- [{item.score}] {item.timestamp} `{item.session_id}` "
        f"`{label}` target={item.target}; reason={item.reason}; text={item.text}"
    )


def append_group(lines: list[str], title: str, items: list[Candidate], limit: int) -> None:
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append("- None.")
        return
    for item in items[:limit]:
        line = candidate_line(item)
        if len(line) > 140:
            lines.append(textwrap.fill(line, width=120, subsequent_indent="  "))
        else:
            lines.append(line)
    if len(items) > limit:
        lines.append(f"- ... {len(items) - limit} more omitted by --limit.")


def write_reports(
    *,
    days: int,
    include_cron: bool,
    include_noise: bool,
    limit: int,
    messages: list[dict],
    sessions: list[dict],
    candidates: list[Candidate],
    shadow_gate: dict,
) -> tuple[Path, Path]:
    stamp = now().strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{stamp}.md"
    json_path = REPORT_DIR / f"{stamp}.json"
    counts = group_counts(candidates)
    by_role = Counter(row.get("role") or "unknown" for row in messages)
    by_source = Counter(row.get("source") or "unknown" for row in messages)

    lines = [
        f"# Hermes Learning Action Queue - {now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Window: last {days} days",
        "",
        "## Decision Gate",
        "",
        "- Next phase: Phase 3b controlled learning and verified self-heal.",
        "- USER.md and MEMORY.md are not automatic long-term-memory targets.",
        "- Low-risk recovered facts are staged in the local Vault and pass existing governance.",
        f"- External memory backend: {shadow_gate['action'].upper()} ({shadow_gate['reason']}).",
        f"- Latest memory-shadow report: {shadow_gate['file'] or 'none'}",
        "",
        "## Summary",
        "",
        f"- Messages scanned: {len(messages)}",
        f"- Sessions scanned: {len(sessions)}",
        f"- Roles: {dict(by_role)}",
        f"- Sources: {dict(by_source)}",
        f"- Cron included: {include_cron}",
        f"- Noise included in report: {include_noise}",
        f"- Operational failure window: last {FAILURE_MAX_AGE_HOURS} hours",
        f"- Stage Vault candidates: {counts['stage_user_memory']}",
        f"- Stage skill-rule candidates: {counts['stage_skill_rule']}",
        f"- Manual review candidates: {counts['manual_review']}",
        f"- Failure signals to investigate: {counts['investigate_failure']}",
        f"- Ignored noise candidates: {counts['ignored_noise']}",
    ]

    by_status: dict[str, list[Candidate]] = {status: [] for status in STATUS_ORDER}
    for item in candidates:
        by_status[item.status].append(item)

    append_group(lines, "Stage For Vault Review", by_status["stage_user_memory"], limit)
    append_group(lines, "Stage As Skill Rules", by_status["stage_skill_rule"], limit)
    append_group(lines, "Manual Review Required", by_status["manual_review"], limit)
    append_group(lines, "Operational Failures To Investigate", by_status["investigate_failure"], limit)
    if include_noise:
        append_group(lines, "Ignored Noise", by_status["ignored_noise"], limit)

    lines.extend(
        [
            "",
            "## Recommended Next Step",
            "",
            "- Keep Hindsight in shadow mode until semantic recall improves on real Max queries.",
            "- Manually promote only Vault candidates that are stable personal facts.",
            "- Convert repeated process rules into focused skills/tests before changing broad prompts.",
            "- Keep medical, medication, finance, legal, baby, and supplement items in manual review.",
            "",
        ]
    )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "generated_at": now().isoformat(),
        "days": days,
        "include_cron": include_cron,
        "include_noise": include_noise,
        "summary": {
            "messages_scanned": len(messages),
            "sessions_scanned": len(sessions),
            "roles": dict(by_role),
            "sources": dict(by_source),
            "counts": counts,
        },
        "shadow_gate": shadow_gate,
        "candidates": [asdict(item) for item in candidates],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a staged Hermes learning action queue.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20, help="Max items shown per markdown section.")
    parser.add_argument("--include-cron", action="store_true", help="Include cron sessions in source history.")
    parser.add_argument("--include-noise", action="store_true", help="Include ignored noise examples.")
    args = parser.parse_args()

    if not STATE_DB.exists():
        raise SystemExit(f"state.db not found: {STATE_DB}")

    days = max(1, args.days)
    limit = max(1, args.limit)
    messages = fetch_messages(days=days, include_cron=args.include_cron)
    sessions = fetch_sessions(days=days, include_cron=args.include_cron)
    candidates = classify(messages, include_noise=args.include_noise)
    shadow_gate = latest_shadow_gate()
    md_path, json_path = write_reports(
        days=days,
        include_cron=args.include_cron,
        include_noise=args.include_noise,
        limit=limit,
        messages=messages,
        sessions=sessions,
        candidates=candidates,
        shadow_gate=shadow_gate,
    )
    print(f"Learning action queue written to {md_path}")
    print(f"Learning action queue JSON written to {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
