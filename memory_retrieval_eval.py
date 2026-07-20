#!/usr/bin/env python3
"""Evaluate production vault retrieval through Qdrant and its local fallback."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys
import time
from pathlib import Path
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
    },
    {
        "name": "home_assistant_air_conditioner",
        "query": "Home Assistant 空调控制脚本 token ha_control.py ha_set_temp.py",
        "expected_any": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
    },
    {
        "name": "skincare_plan",
        "query": "Max 当前护肤方案 CeraVe 阿达帕林 壬二酸 屏障修复",
        "expected_any": ["mem_5015bc9d66b2"],
    },
    {
        "name": "apple_music_windows_bug",
        "query": "Apple Music Windows 无损音质 设置 重置 AAC 256kbps bug",
        "expected_any": ["mem_8c391efcdc4c"],
    },
    {
        "name": "natural_direct_answer_preference",
        "query": "回答我时应该直接给结论，还是列一堆可能性？",
        "expected_any": [
            "mem_4ee64e7f15dd", "mem_aa58b07e80f1",
            "mem_4b1ded36995b", "mem_af0f930195fc",
        ],
    },
    {
        "name": "natural_home_temperature_control",
        "query": "家里的冷气要通过哪个脚本调温？",
        "expected_any": ["mem_f4eb5d1a11a4", "mem_4c726563db61"],
    },
    {
        "name": "natural_skin_barrier",
        "query": "脸上所有东西都有点刺，最近早晚该怎么护理？",
        "expected_any": ["mem_5015bc9d66b2"],
    },
    {
        "name": "natural_music_quality_reset",
        "query": "为什么电脑上的苹果音乐每次重开都变回普通音质？",
        "expected_any": ["mem_8c391efcdc4c"],
    },
    {
        "name": "natural_product_rules",
        "query": "我让你推荐商品时应该遵守什么原则？",
        "expected_any": [
            "mem_7168b61cab3b", "mem_4b1ded36995b",
            "mem_3b11712b2b2e", "mem_7e6f360a6db1",
        ],
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


def is_qdrant_lock_error(message: str) -> bool:
    lowered = str(message or "").lower()
    return "already accessed by another instance of qdrant client" in lowered


def evaluate_case(
    provider,
    case: dict[str, Any],
    *,
    required_mode: str,
) -> dict[str, Any]:
    started = time.monotonic()
    payload = parse_tool_json(provider.handle_tool_call(
        "vault_search",
        {"query": case["query"], "top_k": 10},
    ))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    results = payload.get("results", []) if isinstance(payload.get("results"), list) else []
    memories = [str(item.get("id", "")) for item in results]
    expected_any = list(case["expected_any"])
    matched = [marker for marker in expected_any if marker in memories]
    actual_mode = str(payload.get("mode", "unknown"))
    content_passed = bool(matched)
    mode_passed = actual_mode == required_mode
    return {
        "name": case["name"],
        "query": case["query"],
        "expected_any": expected_any,
        "matched": matched,
        "passed": content_passed and mode_passed,
        "content_passed": content_passed,
        "mode_passed": mode_passed,
        "mode": actual_mode,
        "required_mode": required_mode,
        "elapsed_ms": elapsed_ms,
        "error": payload.get("error") or payload.get("degraded_reason", ""),
        "top_results": [
            {
                "id": item.get("id", ""),
                "score": item.get("score", 0),
                "memory": str(item.get("summary", ""))[:500],
            }
            for item in results[:5]
        ],
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    json_path = REPORT_DIR / f"{stamp}.json"
    md_path = REPORT_DIR / f"{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# Memory Retrieval Eval",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- passed: `{payload['passed']}`",
        f"- cases: `{payload['passed_count']}/{payload['case_count']}`",
        "",
    ]
    for case in payload["cases"]:
        status = "PASS" if case["passed"] else "FAIL"
        lines.extend([
            f"## {case['name']}",
            "",
            f"- status: `{status}`",
            f"- elapsed_ms: `{case['elapsed_ms']}`",
            f"- mode: `{case['mode']}` (required `{case['required_mode']}`)",
            f"- matched: `{', '.join(case['matched']) or 'none'}`",
            f"- expected_any: `{', '.join(case['expected_any'])}`",
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
    payload["report_json"] = str(json_path)
    payload["report_md"] = str(md_path)


def main() -> int:
    ensure_hermes_runtime()
    local_only = "--local-only" in sys.argv
    if local_only:
        sys.argv.remove("--local-only")
    provider = load_provider()
    try:
        if local_only:
            provider._semantic_search = lambda query, top_k: []  # type: ignore[attr-defined]
        required_mode = "local-hybrid" if local_only else "hybrid"
        cases = [
            evaluate_case(provider, case, required_mode=required_mode)
            for case in CASES
        ]
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
    passed_count = sum(1 for case in cases if case["passed"])
    payload = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "passed": passed_count == len(cases),
        "case_count": len(cases),
        "passed_count": passed_count,
        "mode": "local-hybrid" if local_only else "production-hybrid",
        "cases": cases,
    }
    write_report(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
