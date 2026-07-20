#!/usr/bin/env python3
"""Shadow validation for Hermes long-term memory backends.

By default this script is deliberately non-invasive:
- it does not change config.yaml
- it does not write memories
- it does not start/stop launchd jobs
- it writes an audit report under ~/.hermes/reports/memory-shadow/

The optional --lifecycle check creates and deletes an isolated temporary bank.
It never writes to the configured production/shadow bank.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
DEFAULT_URL = "http://127.0.0.1:8100"
DEFAULT_BANK = "hermes"
REPORT_DIR = HERMES_HOME / "reports" / "memory-shadow"
LIFECYCLE_REPORT_DIR = HERMES_HOME / "reports" / "memory-shadow-lifecycle"
BENCHMARK_VERSION = 1
PROMOTION_REQUIRED_DAYS = 7
MAX_BANK_STALE_HOURS = 48.0
MAX_LIFECYCLE_AGE_DAYS = 30
DEFAULT_CASES = [
    (
        "Max audio preference warm vocal loose low frequency",
        ("audio", "warm", "vocal", "low frequency", "音频", "人声", "低频"),
    ),
    (
        "Max skincare plan azelaic acid adapalene sunscreen",
        ("skincare", "azelaic", "adapalene", "sunscreen", "护肤", "壬二酸", "阿达帕林", "防晒"),
    ),
    (
        "Hermes repair verification rule grep validate changes",
        ("verification", "validate", "code path", "修复", "验证", "代码路径"),
    ),
    (
        "RingConn health data automatic daily analysis",
        ("ringconn",),
    ),
    (
        "Max product recommendation rules avoid marketing claims",
        ("marketing claims", "avoid marketing", "营销话术", "材质", "生物力学"),
    ),
]


def now_stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def command_output(args: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, timeout: float = 5.0) -> tuple[bool, Any, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = resp.read().decode("utf-8", errors="replace")
        try:
            return True, json.loads(data), ""
        except json.JSONDecodeError:
            return True, data, ""
    except urllib.error.HTTPError as exc:
        return False, None, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[bool, Any, str]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return True, {}, ""
        try:
            return True, json.loads(raw), ""
        except json.JSONDecodeError:
            return True, raw, ""
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return False, None, f"HTTP {exc.code}: {detail or exc.reason}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def parse_timestamp(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def bank_inventory(
    api_url: str,
    bank_id: str,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    checked_at = now or dt.datetime.now().astimezone()
    ok, payload, error = http_json(
        api_url.rstrip("/") + "/v1/default/banks",
        timeout=10.0,
    )
    result: dict[str, Any] = {
        "ok": ok,
        "exists": False,
        "bank_id": bank_id,
        "fact_count": 0,
        "last_document_at": "",
        "last_document_age_hours": None,
        "fresh": False,
        "max_stale_hours": MAX_BANK_STALE_HOURS,
        "error": error,
    }
    if not ok or not isinstance(payload, dict):
        return result
    banks = payload.get("banks", [])
    bank = next(
        (
            item for item in banks
            if isinstance(item, dict)
            and str(item.get("bank_id", "")) == bank_id
        ),
        None,
    )
    if bank is None:
        return result
    last_document = parse_timestamp(bank.get("last_document_at"))
    age_hours = None
    if last_document is not None:
        age_hours = max(
            0.0,
            (checked_at - last_document).total_seconds() / 3600,
        )
    result.update({
        "exists": True,
        "fact_count": int(bank.get("fact_count", 0) or 0),
        "last_document_at": str(bank.get("last_document_at", "") or ""),
        "last_document_age_hours": (
            round(age_hours, 1) if age_hours is not None else None
        ),
        "fresh": (
            age_hours is not None and age_hours <= MAX_BANK_STALE_HOURS
        ),
    })
    return result


def hindsight_provider_capabilities() -> dict[str, bool]:
    provider_path = (
        HERMES_HOME / "hermes-agent" / "plugins" / "memory"
        / "hindsight" / "__init__.py"
    )
    try:
        source = provider_path.read_text(encoding="utf-8")
    except OSError:
        return {
            "retain": False,
            "recall": False,
            "update": False,
            "forget": False,
        }
    return {
        "retain": '"hindsight_retain"' in source,
        "recall": '"hindsight_recall"' in source,
        "update": '"hindsight_update"' in source,
        "forget": '"hindsight_forget"' in source,
    }


def benchmark_pass_streak(
    report_dir: Path,
    *,
    current_time: dt.datetime,
    current_pass: bool,
) -> int:
    latest_by_day: dict[dt.date, tuple[dt.datetime, bool]] = {}
    for path in report_dir.glob("20*.json"):
        payload = read_json(path)
        benchmark = payload.get("benchmark", {})
        if (
            not isinstance(benchmark, dict)
            or benchmark.get("version") != BENCHMARK_VERSION
            or not benchmark.get("default_suite")
            or not benchmark.get("real_recall")
        ):
            continue
        generated = parse_timestamp(payload.get("generated_at"))
        if generated is None:
            continue
        passed = (
            payload.get("overall") == "PASS"
            and bool(benchmark.get("all_passed"))
        )
        previous = latest_by_day.get(generated.date())
        if previous is None or generated > previous[0]:
            latest_by_day[generated.date()] = (generated, passed)
    latest_by_day[current_time.date()] = (current_time, current_pass)
    streak = 0
    day = current_time.date()
    while latest_by_day.get(day, (current_time, False))[1]:
        streak += 1
        day -= dt.timedelta(days=1)
    return streak


def latest_lifecycle_status(
    report_dir: Path,
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    reports = sorted(report_dir.glob("20*.json"))
    if not reports:
        return {
            "verified": False,
            "age_days": None,
            "report": "",
            "reason": "no isolated lifecycle report",
        }
    path = reports[-1]
    payload = read_json(path)
    generated = parse_timestamp(payload.get("generated_at"))
    age_days = (
        max(0.0, (now - generated).total_seconds() / 86400)
        if generated is not None
        else None
    )
    verified = (
        bool(payload.get("passed"))
        and age_days is not None
        and age_days <= MAX_LIFECYCLE_AGE_DAYS
    )
    return {
        "verified": verified,
        "age_days": round(age_days, 1) if age_days is not None else None,
        "report": str(path),
        "reason": (
            "isolated write/recall/delete passed"
            if verified
            else "lifecycle result missing, failed, or stale"
        ),
    }


def evaluate_promotion_gate(
    *,
    report_dir: Path,
    current_time: dt.datetime,
    current_benchmark_pass: bool,
    bank: dict[str, Any],
    lifecycle: dict[str, Any],
    capabilities: dict[str, bool],
) -> dict[str, Any]:
    streak = benchmark_pass_streak(
        report_dir,
        current_time=current_time,
        current_pass=current_benchmark_pass,
    )
    retrieval_blockers: list[str] = []
    if not current_benchmark_pass:
        retrieval_blockers.append(
            "current default real-recall benchmark is not fully passing"
        )
    if streak < PROMOTION_REQUIRED_DAYS:
        retrieval_blockers.append(
            f"real-recall pass streak is {streak}/{PROMOTION_REQUIRED_DAYS} days"
        )
    if not bank.get("fresh"):
        retrieval_blockers.append(
            "shadow bank has not received fresh documents within "
            f"{int(MAX_BANK_STALE_HOURS)} hours"
        )
    if not lifecycle.get("verified"):
        retrieval_blockers.append(
            "isolated write/recall/delete lifecycle is not verified"
        )
    if not capabilities.get("retain") or not capabilities.get("recall"):
        retrieval_blockers.append(
            "provider does not expose both retain and recall"
        )
    primary_blockers = list(retrieval_blockers)
    if not capabilities.get("update"):
        primary_blockers.append(
            "provider does not expose explicit memory update"
        )
    if not capabilities.get("forget"):
        primary_blockers.append(
            "provider does not expose explicit memory forget/delete"
        )
    return {
        "required_pass_days": PROMOTION_REQUIRED_DAYS,
        "current_pass_streak_days": streak,
        "supplemental_retrieval_eligible": not retrieval_blockers,
        "primary_provider_eligible": not primary_blockers,
        "retrieval_blockers": retrieval_blockers,
        "primary_blockers": primary_blockers,
    }


def config_memory_provider() -> str:
    path = HERMES_HOME / "config.yaml"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_memory = False
    memory_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "memory:":
            in_memory = True
            memory_indent = indent
            continue
        if in_memory and indent <= memory_indent and not line.startswith(" "):
            in_memory = False
        if in_memory and stripped.startswith("provider:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            return value
    return ""


def mem0_local_status() -> dict[str, Any]:
    root = HERMES_HOME / "mem0_qdrant"
    db = root / "collection" / "hermes_memories" / "storage.sqlite"
    meta = read_json(root / "meta.json")
    result: dict[str, Any] = {
        "path": str(root),
        "exists": root.exists(),
        "bytes": directory_size(root) if root.exists() else 0,
        "point_count": None,
        "vector_size": None,
        "distance": None,
    }
    try:
        collection = meta.get("collections", {}).get("hermes_memories", {})
        vectors = collection.get("vectors", {})
        result["vector_size"] = vectors.get("size")
        result["distance"] = vectors.get("distance")
    except Exception:
        pass
    if db.exists():
        try:
            with sqlite3.connect(db) as conn:
                result["point_count"] = conn.execute("select count(*) from points").fetchone()[0]
        except Exception as exc:
            result["point_error"] = f"{type(exc).__name__}: {exc}"
    return result


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    if path.is_file():
        return path.stat().st_size
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def fmt_size(num: int | None) -> str:
    if num is None:
        return "unknown"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


def hindsight_config() -> dict[str, Any]:
    candidates = [
        HERMES_HOME / "hindsight" / "config.json",
        HERMES_HOME / "hindsight" / "config.json.bak",
        Path.home() / ".hindsight" / "hermes.json",
    ]
    merged: dict[str, Any] = {}
    for path in candidates:
        data = read_json(path)
        if not data:
            continue
        merged.setdefault("_sources", []).append(str(path))
        for key, value in data.items():
            if key in {"api_key", "llm_api_key", "llmApiKey"}:
                continue
            merged.setdefault(key, value)
    if "hindsightApiUrl" in merged and "api_url" not in merged:
        merged["api_url"] = merged["hindsightApiUrl"]
    if "bankId" in merged and "bank_id" not in merged:
        merged["bank_id"] = merged["bankId"]
    return merged


def run_recall(
    api_url: str,
    bank_id: str,
    query: str,
    *,
    expected_any: tuple[str, ...] = (),
) -> dict[str, Any]:
    endpoint = (
        api_url.rstrip("/")
        + "/v1/default/banks/"
        + urllib.parse.quote(bank_id, safe="")
        + "/memories/recall"
    )
    request_payload = json.dumps({
        "query": query,
        "types": ["observation"],
        "max_tokens": 900,
        "budget": "low",
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_payload,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        started = time.time()
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310
            raw = json.loads(response.read().decode("utf-8"))
        elapsed = time.time() - started
        results = raw.get("results", []) if isinstance(raw, dict) else []
        rendered: list[str] = []
        searchable: list[str] = []
        for item in results[:5]:
            if not isinstance(item, dict):
                text = str(item)
                rendered.append(text[:500])
                searchable.append(text.lower())
                continue
            text = (
                item.get("content") or item.get("text")
                or item.get("memory") or item.get("fact") or str(item)
            )
            score = item.get("score")
            prefix = f"score={score} " if score is not None else ""
            rendered.append(prefix + str(text).replace("\n", " ")[:500])
            searchable.append(str(text).lower())
        matched = sorted({
            marker for marker in expected_any
            if any(marker.lower() in text for text in searchable)
        })
        quality_ok = bool(results) and (not expected_any or bool(matched))
        return {
            "ok": True,
            "quality_ok": quality_ok,
            "elapsed_s": round(elapsed, 2),
            "count": len(results),
            "matched": matched,
            "expected_any": list(expected_any),
            "items": rendered,
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "quality_ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "quality_ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_lifecycle_check(api_url: str) -> dict[str, Any]:
    """Verify retain/recall/delete against a disposable isolated bank."""
    started_at = dt.datetime.now().astimezone()
    suffix = uuid.uuid4().hex[:10]
    bank_id = f"hermes-shadow-lifecycle-{suffix}"
    marker = f"cobalt-{suffix}"
    bank_url = (
        api_url.rstrip("/")
        + "/v1/default/banks/"
        + urllib.parse.quote(bank_id, safe="")
    )
    steps: dict[str, dict[str, Any]] = {}
    created = False
    try:
        ok, _, error = request_json(
            bank_url,
            method="PUT",
            payload={
                "name": bank_id,
                "enable_observations": False,
            },
            timeout=30.0,
        )
        steps["create_bank"] = {"ok": ok, "error": error}
        created = ok
        if ok:
            ok, retain_payload, error = request_json(
                bank_url + "/memories",
                method="POST",
                payload={
                    "items": [{
                        "content": (
                            "The isolated Hermes lifecycle verification "
                            f"code is {marker}."
                        ),
                        "context": "automated isolated lifecycle test",
                        "document_id": f"lifecycle-{suffix}",
                        "tags": ["hermes-shadow-lifecycle"],
                    }],
                    "async": False,
                },
                timeout=120.0,
            )
            reported_success = (
                bool(retain_payload.get("success"))
                if isinstance(retain_payload, dict)
                else False
            )
            list_ok, list_payload, list_error = request_json(
                bank_url + "/memories/list?limit=20&offset=0",
                timeout=15.0,
            )
            materialized_count = (
                int(list_payload.get("total", 0) or 0)
                if list_ok and isinstance(list_payload, dict)
                else 0
            )
            steps["retain"] = {
                "ok": ok and reported_success and materialized_count > 0,
                "request_ok": ok,
                "reported_success": reported_success,
                "materialized_count": materialized_count,
                "error": error or list_error,
                "response_type": type(retain_payload).__name__,
            }
        if steps.get("retain", {}).get("ok"):
            ok, recall_payload, error = request_json(
                bank_url + "/memories/recall",
                method="POST",
                payload={
                    "query": (
                        "What is the isolated Hermes lifecycle "
                        "verification code?"
                    ),
                    "types": ["world", "experience", "observation"],
                    "max_tokens": 600,
                    "budget": "low",
                },
                timeout=60.0,
            )
            results = (
                recall_payload.get("results", [])
                if ok and isinstance(recall_payload, dict)
                else []
            )
            searchable = []
            for item in results:
                if isinstance(item, dict):
                    searchable.append(str(
                        item.get("content")
                        or item.get("text")
                        or item.get("memory")
                        or item.get("fact")
                        or item
                    ).lower())
                else:
                    searchable.append(str(item).lower())
            marker_found = any(marker in text for text in searchable)
            steps["recall"] = {
                "ok": ok and marker_found,
                "request_ok": ok,
                "marker_found": marker_found,
                "result_count": len(results),
                "error": error,
            }
    finally:
        ok, _, error = request_json(
            bank_url,
            method="DELETE",
            timeout=30.0,
        )
        steps["delete_bank"] = {
            "ok": ok if created else True,
            "attempted": True,
            "error": error if created else "",
        }
        inventory_ok, inventory_payload, inventory_error = http_json(
            api_url.rstrip("/") + "/v1/default/banks",
            timeout=10.0,
        )
        banks = (
            inventory_payload.get("banks", [])
            if inventory_ok and isinstance(inventory_payload, dict)
            else []
        )
        absent = all(
            not isinstance(item, dict)
            or str(item.get("bank_id", "")) != bank_id
            for item in banks
        )
        steps["cleanup_verified"] = {
            "ok": inventory_ok and absent,
            "bank_absent": absent,
            "error": inventory_error,
        }
    passed = all(
        steps.get(name, {}).get("ok")
        for name in (
            "create_bank",
            "retain",
            "recall",
            "delete_bank",
            "cleanup_verified",
        )
    )
    payload = {
        "generated_at": started_at.isoformat(timespec="seconds"),
        "passed": passed,
        "isolated_bank": bank_id,
        "marker": marker,
        "steps": steps,
    }
    LIFECYCLE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = (
        LIFECYCLE_REPORT_DIR
        / f"{started_at.strftime('%Y%m%d-%H%M%S')}.json"
    )
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    payload["report"] = str(report_path)
    return payload


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow-check Hermes memory backends.")
    parser.add_argument("--url", default="", help="Hindsight API URL. Default: config or http://127.0.0.1:8100")
    parser.add_argument("--bank", default="", help="Hindsight bank id. Default: config or hermes")
    parser.add_argument("--query", action="append", default=[], help="Recall query. Repeatable.")
    parser.add_argument("--no-recall", action="store_true", help="Only run health/config checks.")
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Create and delete an isolated bank to verify write/recall/delete.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when shadow recall quality warns.")
    args = parser.parse_args()

    run_time = dt.datetime.now().astimezone()
    run_stamp = run_time.strftime("%Y%m%d-%H%M%S")
    cfg = hindsight_config()
    api_url = args.url or cfg.get("api_url") or DEFAULT_URL
    bank_id = args.bank or cfg.get("bank_id") or DEFAULT_BANK
    host = api_url.replace("http://", "").replace("https://", "").split("/", 1)[0].split(":", 1)[0]
    try:
        port = int(api_url.rsplit(":", 1)[1].split("/", 1)[0])
    except Exception:
        port = 443 if api_url.startswith("https://") else 80

    provider = config_memory_provider()
    launch_code, launch_state = command_output(["launchctl", "list", "ai.hermes.hindsight"])
    port_open = tcp_open(host, port)
    health_ok, health_payload, health_error = http_json(api_url.rstrip("/") + "/health")
    version_ok, version_payload, version_error = http_json(api_url.rstrip("/") + "/version")
    bank = bank_inventory(api_url, bank_id, now=run_time)
    capabilities = hindsight_provider_capabilities()
    mem0 = mem0_local_status()
    disk = shutil.disk_usage(str(HERMES_HOME))

    cases = (
        [(query, ()) for query in args.query]
        if args.query
        else DEFAULT_CASES
    )
    recall_results: dict[str, dict[str, Any]] = {}
    if not args.no_recall and health_ok and isinstance(health_payload, dict) and health_payload.get("status") == "healthy":
        for query, expected_any in cases:
            recall_results[query] = run_recall(
                api_url,
                bank_id,
                query,
                expected_any=expected_any,
            )

    recall_ok = (
        args.no_recall
        or (
            len(recall_results) == len(cases)
            and all(
                result.get("ok") and result.get("quality_ok")
                for result in recall_results.values()
            )
        )
    )
    status_line = "PASS" if health_ok and port_open and recall_ok else "WARN"
    default_suite = not args.query and not args.no_recall
    current_benchmark_pass = (
        default_suite
        and status_line == "PASS"
        and len(recall_results) == len(DEFAULT_CASES)
    )
    lifecycle_run: dict[str, Any] | None = None
    if (
        args.lifecycle
        and health_ok
        and isinstance(health_payload, dict)
        and health_payload.get("status") == "healthy"
    ):
        lifecycle_run = run_lifecycle_check(api_url)
    lifecycle = latest_lifecycle_status(
        LIFECYCLE_REPORT_DIR,
        now=run_time,
    )
    gate = evaluate_promotion_gate(
        report_dir=REPORT_DIR,
        current_time=run_time,
        current_benchmark_pass=current_benchmark_pass,
        bank=bank,
        lifecycle=lifecycle,
        capabilities=capabilities,
    )
    lines = [
        f"# Hermes Memory Shadow Check - {run_stamp}",
        "",
        f"Overall: {status_line}",
        "",
        "## Active Hermes Memory",
        "",
        f"- config.yaml memory.provider: `{provider or '(built-in only)'}`",
        "- Production external memory is not enabled by this script.",
        "",
        "## Hindsight",
        "",
        f"- API URL: `{api_url}`",
        f"- Bank: `{bank_id}`",
        f"- Config sources: {', '.join(cfg.get('_sources', [])) or 'none'}",
        f"- TCP open: {port_open}",
        f"- launchctl exit: {launch_code}",
        f"- health: {'ok' if health_ok else 'failed'} {json.dumps(health_payload, ensure_ascii=False) if health_ok else health_error}",
        f"- version: {'ok' if version_ok else 'failed'} {json.dumps(version_payload, ensure_ascii=False) if version_ok else version_error}",
        "",
        "## Bank Data Freshness",
        "",
        f"- Exists: {bank.get('exists')}",
        f"- Facts: {bank.get('fact_count')}",
        f"- Last document: {bank.get('last_document_at') or 'none'}",
        f"- Last document age: {bank.get('last_document_age_hours')} hours",
        f"- Fresh within {int(MAX_BANK_STALE_HOURS)} hours: {bank.get('fresh')}",
        "",
        "## Legacy Local-Path mem0/Qdrant Residue",
        "",
        f"- Exists: {mem0['exists']}",
        f"- Size: {fmt_size(mem0['bytes'])}",
        f"- Points: {mem0.get('point_count')}",
        f"- Vector size: {mem0.get('vector_size')}",
        f"- Distance: {mem0.get('distance')}",
        "- Status: retained as historical data; not an active Hermes provider.",
        "",
        "## Mac Mini Capacity Snapshot",
        "",
        f"- Hermes volume free: {fmt_size(disk.free)}",
        f"- Hermes home size: {fmt_size(directory_size(HERMES_HOME))}",
        f"- pg0 size: {fmt_size(directory_size(Path.home() / '.pg0'))}",
        f"- HuggingFace cache size: {fmt_size(directory_size(Path.home() / '.cache' / 'huggingface'))}",
        "",
        "## Shadow Recall",
        "",
    ]
    if not recall_results:
        lines.append("- Recall not run because Hindsight is not healthy or --no-recall was set.")
    else:
        for query, result in recall_results.items():
            lines.append(f"### {query}")
            if not result.get("ok"):
                lines.append(f"- ERROR: {result.get('error')}")
                lines.append("")
                continue
            lines.append(f"- elapsed: {result.get('elapsed_s')}s")
            lines.append(f"- result count: {result.get('count')}")
            lines.append(f"- quality: {'pass' if result.get('quality_ok') else 'warn'}")
            if result.get("expected_any"):
                lines.append(f"- matched markers: {', '.join(result.get('matched', [])) or 'none'}")
            for item in result.get("items", []):
                wrapped = textwrap.fill(item, width=110, subsequent_indent="  ")
                lines.append(f"- {wrapped}")
            lines.append("")
    lines.extend(
        [
            "## Isolated Lifecycle",
            "",
            f"- Verified: {lifecycle.get('verified')}",
            f"- Age: {lifecycle.get('age_days')} days",
            f"- Report: {lifecycle.get('report') or 'none'}",
            (
                f"- Current run passed: {lifecycle_run.get('passed')}"
                if lifecycle_run is not None
                else "- Current run: not requested"
            ),
            "",
            "## Promotion Gate",
            "",
            (
                "- Real-recall pass streak: "
                f"{gate['current_pass_streak_days']}/"
                f"{gate['required_pass_days']} days"
            ),
            (
                "- Supplemental retrieval eligible: "
                f"{gate['supplemental_retrieval_eligible']}"
            ),
            (
                "- Primary provider eligible: "
                f"{gate['primary_provider_eligible']}"
            ),
            (
                "- Provider capabilities: "
                + ", ".join(
                    f"{name}={enabled}"
                    for name, enabled in capabilities.items()
                )
            ),
            "- Primary blockers:",
        ]
    )
    lines.extend(
        f"  - {blocker}"
        for blocker in gate["primary_blockers"]
    )
    if not gate["primary_blockers"]:
        lines.append("  - none")
    lines.extend(
        [
            "## Recommendation",
            "",
            "- Keep `memory.provider` unchanged while the promotion gate is blocked.",
            "- Health-only historical reports do not count toward the real-recall streak.",
            "- Do not delete `mem0_qdrant`; it may be useful for migration.",
            "- If Hindsight health is unhealthy, fix launchd/wrapper before enabling provider.",
            "",
        ]
    )

    report_payload = {
        "generated_at": run_time.isoformat(timespec="seconds"),
        "overall": status_line,
        "active_memory_provider": provider or "(built-in only)",
        "hindsight": {
            "api_url": api_url,
            "bank_id": bank_id,
            "port_open": port_open,
            "health_ok": health_ok,
            "health": health_payload if health_ok else health_error,
            "version_ok": version_ok,
            "version": version_payload if version_ok else version_error,
        },
        "bank": bank,
        "benchmark": {
            "version": BENCHMARK_VERSION,
            "default_suite": default_suite,
            "real_recall": bool(recall_results),
            "case_count": len(cases),
            "passed_count": sum(
                bool(result.get("ok") and result.get("quality_ok"))
                for result in recall_results.values()
            ),
            "all_passed": current_benchmark_pass,
            "results": recall_results,
        },
        "lifecycle": lifecycle,
        "lifecycle_run": lifecycle_run,
        "provider_capabilities": capabilities,
        "promotion_gate": gate,
    }
    report_path = REPORT_DIR / f"{run_stamp}.md"
    json_path = REPORT_DIR / f"{run_stamp}.json"
    write_report(report_path, "\n".join(lines))
    json_path.write_text(
        json.dumps(
            report_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{status_line}: memory shadow report written to {report_path}; "
        f"JSON: {json_path}"
    )
    if not health_ok or not port_open:
        print("Hindsight is not healthy; production provider remains unchanged.")
        return 2
    if status_line != "PASS" and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
