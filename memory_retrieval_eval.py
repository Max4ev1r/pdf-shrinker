#!/usr/bin/env python3
"""Evaluate precision and recall of the production local Vault retrieval path."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR", str(HERMES_HOME / "hermes-agent"))
).expanduser()
HERMES_PYTHON = HERMES_AGENT_DIR / ".venv" / "bin" / "python"
REPORT_DIR = HERMES_HOME / "reports" / "memory-retrieval"

CASES = [
    {
        "name": "communication_preference",
        "query": "Max 的沟通偏好 唯一推荐 明确理由 修复深度验证",
        "expected_any": ["mem_4ee64e7f15dd", "mem_aa58b07e80f1"],
        "allowed_ids": [
            "mem_4ee64e7f15dd", "mem_aa58b07e80f1",
            "mem_4d24f96563b4", "mem_0b15485647b1",
            "mem_af0f930195fc",
        ],
    },
    {
        "name": "home_assistant_air_conditioner",
        "query": "Home Assistant 空调控制脚本 token ha_control.py ha_set_temp.py",
        "expected_any": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
        "allowed_ids": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
    },
    {
        "name": "skincare_plan",
        "query": "Max 当前护肤方案 CeraVe 阿达帕林 壬二酸 屏障修复",
        "expected_any": ["mem_5015bc9d66b2"],
        "allowed_ids": ["mem_5015bc9d66b2"],
    },
    {
        "name": "apple_music_windows_bug",
        "query": "Apple Music Windows 无损音质 设置 重置 AAC 256kbps bug",
        "expected_any": ["mem_8c391efcdc4c"],
        "allowed_ids": ["mem_8c391efcdc4c"],
    },
    {
        "name": "natural_direct_answer_preference",
        "query": "回答我时应该直接给结论，还是列一堆可能性？",
        "expected_any": [
            "mem_4ee64e7f15dd", "mem_aa58b07e80f1",
            "mem_4b1ded36995b", "mem_af0f930195fc",
        ],
        "allowed_ids": [
            "mem_4ee64e7f15dd", "mem_aa58b07e80f1",
            "mem_4b1ded36995b", "mem_af0f930195fc",
            "mem_4d24f96563b4", "mem_0b15485647b1",
            "mem_7168b61cab3b",
        ],
    },
    {
        "name": "natural_home_temperature_control",
        "query": "家里的冷气要通过哪个脚本调温？",
        "expected_any": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
        "allowed_ids": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
    },
    {
        "name": "natural_skin_barrier",
        "query": "脸上所有东西都有点刺，最近早晚该怎么护理？",
        "expected_any": ["mem_5015bc9d66b2"],
        "allowed_ids": ["mem_5015bc9d66b2"],
    },
    {
        "name": "natural_music_quality_reset",
        "query": "为什么电脑上的苹果音乐每次重开都变回普通音质？",
        "expected_any": ["mem_8c391efcdc4c"],
        "allowed_ids": ["mem_8c391efcdc4c"],
    },
    {
        "name": "natural_product_rules",
        "query": "我让你推荐商品时应该遵守什么原则？",
        "expected_any": [
            "mem_7168b61cab3b", "mem_4b1ded36995b",
            "mem_3b11712b2b2e", "mem_7e6f360a6db1",
        ],
        "allowed_ids": [
            "mem_7168b61cab3b", "mem_4b1ded36995b",
            "mem_3b11712b2b2e", "mem_7e6f360a6db1",
            "mem_f61b63101c55",
        ],
    },
    {
        "name": "negative_greeting",
        "query": "你好，今天怎么样？",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
    {
        "name": "negative_weather",
        "query": "无锡今天会下雨吗？",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
    {
        "name": "negative_arithmetic",
        "query": "帮我算一下 37 乘以 19",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
    {
        "name": "negative_programming",
        "query": "解释一下 Python 的 async await",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
    {
        "name": "negative_translation",
        "query": "把 good morning 翻译成中文",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
    {
        "name": "negative_one_off_task",
        "query": "帮我把下面这句话改得更通顺",
        "expected_any": [],
        "allowed_ids": [],
        "expect_empty": True,
    },
]


def now_stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def ensure_hermes_runtime() -> None:
    if os.environ.get("MEMORY_RETRIEVAL_EVAL_NO_REEXEC"):
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
    env["MEMORY_RETRIEVAL_EVAL_NO_REEXEC"] = "1"
    os.execve(str(HERMES_PYTHON), [str(HERMES_PYTHON), __file__, *sys.argv[1:]], env)


def load_provider():
    if str(HERMES_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT_DIR))
    module = importlib.import_module("plugins.memory")
    provider = module.load_memory_provider("vault")
    if provider is None:
        raise RuntimeError("Vault memory provider could not be loaded")
    provider.initialize("memory-retrieval-eval", hermes_home=str(HERMES_HOME), platform="memory-retrieval", user_id="max")
    return provider


def parse_tool_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except Exception:
        return {"error": raw}
    return payload if isinstance(payload, dict) else {"result": payload}


def evaluate_case(
    provider,
    case: dict[str, Any],
    *,
    accepted_modes: set[str],
    production: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    automatic = bool(case.get("automatic", case.get("expect_empty", False)))
    context = ""
    if automatic:
        context = provider.prefetch(case["query"], session_id="memory-retrieval-eval")
        ids = re.findall(r"^- \[([^\]]+)\]", context, re.M)
        payload = {
            "results": [{"id": marker} for marker in ids],
            "mode": provider._vault.local_search_mode(),
        }
    else:
        payload = parse_tool_json(provider.handle_tool_call(
            "vault_search",
            {"query": case["query"], "top_k": 10},
        ))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    results = payload.get("results", []) if isinstance(payload.get("results"), list) else []
    memories = [str(item.get("id", "")) for item in results]
    expected_any = list(case["expected_any"])
    allowed_ids = set(case.get("allowed_ids", expected_any))
    matched = [marker for marker in expected_any if marker in memories]
    actual_mode = str(payload.get("mode", "unknown"))
    expect_empty = bool(case.get("expect_empty"))
    content_passed = (not context if automatic else not memories) if expect_empty else bool(matched)
    unknown_ids = [marker for marker in memories if marker not in allowed_ids]
    # Production is an open corpus: unlabelled results are neither known
    # errors nor known-relevant. Closed-corpus precision remains a required
    # part of the overall evaluation below, never a production ID whitelist.
    precision_passed = (
        content_passed if expect_empty else
        None if production else not unknown_ids
    )
    temporal_passed = all(label in context for label in case.get("required_context", []))
    mode_passed = actual_mode in accepted_modes
    error = payload.get("error") or payload.get("degraded_reason", "")
    return {
        "name": case["name"],
        "query": case["query"],
        "expected_any": expected_any,
        "matched": matched,
        "passed": (
            content_passed
            and precision_passed is not False
            and temporal_passed
            and mode_passed
            and not error
        ),
        "content_passed": content_passed,
        "precision_passed": precision_passed,
        "precision_scope": "open-corpus-unjudged" if production and not expect_empty else "closed-corpus",
        "unjudged_ids": unknown_ids if production else [],
        "automatic": automatic,
        "temporal_passed": temporal_passed,
        "mode_passed": mode_passed,
        "mode": actual_mode,
        "accepted_modes": sorted(accepted_modes),
        "expect_empty": expect_empty,
        "allowed_ids": sorted(allowed_ids),
        "elapsed_ms": elapsed_ms,
        "error": error,
        "top_results": [
            {
                "id": item.get("id", ""),
                "score": item.get("score", 0),
                "memory": str(item.get("summary", ""))[:500],
            }
            for item in results[:3]
        ],
    }


def evaluate_backends(provider) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        vector = next(iter(
            provider._vault.get_local_embedder().query_embed(
                ["Hermes 本地长期记忆向量检索健康检查"]
            )
        ))
        dimensions = len(vector)
        checks.append({
            "name": "local_vector",
            "passed": dimensions > 0,
            "dimensions": dimensions,
            "error": "",
        })
    except Exception as exc:
        checks.append({
            "name": "local_vector",
            "passed": False,
            "dimensions": 0,
            "error": f"{type(exc).__name__}: {exc}",
        })

    try:
        records = provider._vault.read_jsonl(provider._vault.RECORDS_PATH)
        provider._vault.ensure_local_index(records)
        with sqlite3.connect(provider._vault.LOCAL_INDEX_PATH) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            metadata = dict(
                conn.execute("SELECT key,value FROM index_meta").fetchall()
            )
        checks.append({
            "name": "local_index",
            "passed": (
                integrity == "ok"
                and metadata.get("embedding_status") == "ready"
            ),
            "integrity": integrity,
            "embedding_status": metadata.get("embedding_status", ""),
            "error": "",
        })
    except Exception as exc:
        checks.append({
            "name": "local_index",
            "passed": False,
            "integrity": "unknown",
            "embedding_status": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
        })
    return checks


def write_report(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    json_path = REPORT_DIR / f"{stamp}.json"
    md_path = REPORT_DIR / f"{stamp}.md"
    payload["report_json"] = str(json_path)
    payload["report_md"] = str(md_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Memory Retrieval Eval",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- passed: `{payload['passed']}`",
        f"- cases: `{payload['passed_count']}/{payload['case_count']}`",
        f"- backends: `{payload['backend_passed_count']}/{payload['backend_count']}`",
        f"- warm p95: `{payload['latency_p95_ms']}ms` (limit `{payload['latency_limit_ms']}ms`)",
        "",
        "## Backend Checks",
        "",
    ]
    for check in payload["backend_checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        detail = (
            f"dimensions={check['dimensions']}"
            if "dimensions" in check
            else (
                f"integrity={check['integrity']} "
                f"embedding={check['embedding_status']}"
            )
        )
        lines.append(f"- `{status}` `{check['name']}` {detail}")
        if check.get("error"):
            lines.append(f"  - error: `{check['error']}`")
    lines.append("")
    for case in payload["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        lines.extend([
            f"## {case['name']}",
            "",
            f"- status: `{status}`",
            f"- elapsed_ms: `{case['elapsed_ms']}`",
            f"- mode: `{case['mode']}` (accepted `{', '.join(case['accepted_modes'])}`)",
            f"- matched: `{', '.join(case['matched']) or 'none'}`",
            f"- expected_any: `{', '.join(case['expected_any'])}`",
            f"- precision_passed: `{case['precision_passed']}`",
            f"- precision_scope: `{case['precision_scope']}`",
            f"- retrieval_path: `{'automatic prefetch' if case['automatic'] else 'explicit search'}`",
            f"- unjudged_ids: `{', '.join(case['unjudged_ids']) or 'none'}`",
            f"- temporal_passed: `{case['temporal_passed']}`",
            f"- allowed_ids: `{', '.join(case['allowed_ids']) or 'none'}`",
            f"- query: `{case['query']}`",
            "",
        ])
        if case.get("error"):
            lines.extend([f"- error: `{case['error']}`", ""])
        for idx, result in enumerate(case["top_results"], 1):
            memory = result["memory"].replace("\n", " ")
            lines.append(f"{idx}. `{result['id']}` score=`{result['score']}` {memory}")
        lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def evaluate_closed_corpus(provider) -> list[dict[str, Any]]:
    """Exercise the real index and plugin on a disposable labelled corpus.

    This never initializes a Vault, writes authority data, or rebuilds the
    production index. Only the existing local embedder is shared.
    """
    from memory_vault_lib.local_index import LocalSearchIndex

    facts = {
        "fixture_project": "我的 Pine 项目使用 Python asyncio 和 SQLite；部署在单机上，禁止引入 Redis 服务。",
        "fixture_style": "我的表达偏好：回答先给结论，再给理由，中文为主，避免冗长铺垫。",
        "fixture_skin_current": "[CURRENT] 我的当前护肤方案：温和洁面和保湿，暂停刺激性产品。",
        "fixture_skin_history": "[HISTORICAL/current-to-confirm] 我的历史护肤记录：曾经使用酸类产品；不能作为当前方案。",
        "fixture_food": "我的饮食限制：花生过敏，餐厅推荐必须排除花生。",
        "fixture_travel": "我的旅行计划：下个月去京都，优先参观寺庙和博物馆。",
    }
    records = [
        {"id": marker, "title": body, "summary": body, "body": body,
         "status": "active", "topic": "fixture", "tags": [],
         "updated_at": "2026-01-01", "content_hash": marker}
        for marker, body in facts.items()
    ]
    cases = [
        {"name": "precision_project", "query": "我的 Pine 项目采用什么数据库和异步实现？",
         "expected_any": ["fixture_project"], "automatic": True},
        {"name": "precision_style", "query": "按我的表达偏好组织回答，应该怎么写？",
         "expected_any": ["fixture_style"], "automatic": True},
        {"name": "personalized_programming", "query": "结合我现在 Pine 的 Python 实现解释 async await",
         "expected_any": ["fixture_project"], "automatic": True},
        {"name": "personalized_translation", "query": "按我平时喜欢的表达方式翻译这段话",
         "expected_any": ["fixture_style"], "automatic": True},
        {"name": "precision_skin_temporal", "query": "我的当前和历史护肤方案有什么区别？",
         "expected_any": ["fixture_skin_current", "fixture_skin_history"],
         "automatic": True, "required_context": ["[CURRENT]", "[HISTORICAL/current-to-confirm]", "不能作为当前方案"]},
        {"name": "precision_food", "query": "我的饮食限制是什么？",
         "expected_any": ["fixture_food"], "automatic": True},
        {"name": "explicit_search_preserved", "query": "Pine 项目采用什么数据库和异步实现？",
         "expected_any": ["fixture_project"]},
        {"name": "automatic_generic_programming", "query": "解释一下 Python 的 async await", "expected_any": [], "expect_empty": True},
        {"name": "automatic_generic_translation", "query": "把 good morning 翻译成中文", "expected_any": [], "expect_empty": True},
        {"name": "automatic_generic_food", "query": "解释一下花生过敏的定义", "expected_any": [], "expect_empty": True},
        {"name": "automatic_quoted_personal_text", "query": '翻译“我的项目使用 Python”', "expected_any": [], "expect_empty": True},
    ]
    with tempfile.TemporaryDirectory(prefix="memory-retrieval-fixture-") as directory:
        index = LocalSearchIndex(
            path=Path(directory) / "index.sqlite3", schema_version=1,
            embedding_model=provider._vault.LOCAL_EMBEDDING_MODEL,
            alias_groups=provider._vault.SEARCH_ALIAS_GROUPS,
            embedder_factory=provider._vault.get_local_embedder,
            ensure_layout=lambda: None, lock_factory=nullcontext,
        )
        index.build(records, index.path)
        fixture = type(provider)()
        fixture._vault = SimpleNamespace(
            local_search=lambda query, top_k=10: index.search(records, query, top_k=top_k),
            local_search_mode=lambda: index.health(records)["mode"],
        )
        # A closed corpus has no personal session history outside its labels.
        fixture._search_session_history = lambda query, top_k=3: []
        return [evaluate_case(fixture, case, accepted_modes={"local-hybrid"}) for case in cases]


def main() -> int:
    ensure_hermes_runtime()
    provider = load_provider()
    try:
        backend_checks = evaluate_backends(provider)
        accepted_modes = {"local-hybrid"}
        cases = [
            evaluate_case(provider, case, accepted_modes=accepted_modes, production=True)
            for case in CASES
        ]
        cases.extend(evaluate_closed_corpus(provider))
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
    passed_count = sum(1 for case in cases if case["passed"])
    backend_passed_count = sum(1 for check in backend_checks if check["passed"])
    elapsed = sorted(case["elapsed_ms"] for case in cases)
    p95_index = max(0, (95 * len(elapsed) + 99) // 100 - 1)
    latency_p95_ms = elapsed[p95_index] if elapsed else 0
    latency_limit_ms = 50
    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "passed": (
            passed_count == len(cases)
            and backend_passed_count == len(backend_checks)
            and latency_p95_ms <= latency_limit_ms
        ),
        "case_count": len(cases),
        "passed_count": passed_count,
        "backend_count": len(backend_checks),
        "backend_passed_count": backend_passed_count,
        "backend_checks": backend_checks,
        "latency_p95_ms": latency_p95_ms,
        "latency_limit_ms": latency_limit_ms,
        "mode": "production-local-hybrid",
        "cases": cases,
    }
    write_report(payload)
    print(json.dumps({
        "passed": payload["passed"],
        "cases": f"{passed_count}/{len(cases)}",
        "backends": f"{backend_passed_count}/{len(backend_checks)}",
        "failed_cases": [
            case["name"] for case in cases if not case["passed"]
        ],
        "failed_backends": [
            check["name"] for check in backend_checks if not check["passed"]
        ],
        "latency_p95_ms": latency_p95_ms,
        "latency_limit_ms": latency_limit_ms,
        "report_json": payload["report_json"],
        "report_md": payload["report_md"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
