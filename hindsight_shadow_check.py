#!/usr/bin/env python3
"""Shadow validation for Hermes long-term memory backends.

This script is deliberately non-invasive:
- it does not change config.yaml
- it does not write memories
- it does not start/stop launchd jobs
- it writes an audit report under ~/.hermes/reports/memory-shadow/
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
import urllib.request
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
DEFAULT_URL = "http://127.0.0.1:8100"
DEFAULT_BANK = "hermes"
DEFAULT_QUERIES = [
    "Max audio preference warm vocal loose low frequency",
    "Max skincare plan azelaic acid adapalene sunscreen",
    "Hermes repair verification rule grep validate changes",
    "RingConn health data automatic daily analysis",
    "Max product recommendation rules avoid marketing claims",
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
        merged.update({k: v for k, v in data.items() if k not in {"api_key", "llm_api_key", "llmApiKey"}})
    if "hindsightApiUrl" in merged and "api_url" not in merged:
        merged["api_url"] = merged["hindsightApiUrl"]
    if "bankId" in merged and "bank_id" not in merged:
        merged["bank_id"] = merged["bankId"]
    return merged


def run_recall(api_url: str, bank_id: str, query: str) -> dict[str, Any]:
    try:
        from hindsight_client import Hindsight
    except Exception as exc:
        return {"ok": False, "error": f"hindsight_client import failed: {exc}"}
    try:
        client = Hindsight(base_url=api_url, timeout=20.0)
        started = time.time()
        response = client.recall(
            bank_id=bank_id,
            query=query,
            types=["observation"],
            max_tokens=900,
            budget="low",
        )
        elapsed = time.time() - started
        raw = response.model_dump() if hasattr(response, "model_dump") else response
        results = raw.get("results", []) if isinstance(raw, dict) else []
        rendered: list[str] = []
        for item in results[:5]:
            if not isinstance(item, dict):
                rendered.append(str(item)[:500])
                continue
            text = item.get("content") or item.get("text") or item.get("memory") or item.get("fact") or str(item)
            score = item.get("score")
            prefix = f"score={score} " if score is not None else ""
            rendered.append(prefix + str(text).replace("\n", " ")[:500])
        client.close()
        return {"ok": True, "elapsed_s": round(elapsed, 2), "count": len(results), "items": rendered}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow-check Hermes memory backends.")
    parser.add_argument("--url", default="", help="Hindsight API URL. Default: config or http://127.0.0.1:8100")
    parser.add_argument("--bank", default="", help="Hindsight bank id. Default: config or hermes")
    parser.add_argument("--query", action="append", default=[], help="Recall query. Repeatable.")
    parser.add_argument("--no-recall", action="store_true", help="Only run health/config checks.")
    args = parser.parse_args()

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
    mem0 = mem0_local_status()
    disk = shutil.disk_usage(str(HERMES_HOME))

    queries = args.query or DEFAULT_QUERIES
    recall_results: dict[str, dict[str, Any]] = {}
    if not args.no_recall and health_ok and isinstance(health_payload, dict) and health_payload.get("status") == "healthy":
        for query in queries:
            recall_results[query] = run_recall(api_url, bank_id, query)

    status_line = "PASS" if health_ok and port_open else "WARN"
    lines = [
        f"# Hermes Memory Shadow Check - {now_stamp()}",
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
        "## mem0/Qdrant Residue",
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
            for item in result.get("items", []):
                wrapped = textwrap.fill(item, width=110, subsequent_indent="  ")
                lines.append(f"- {wrapped}")
            lines.append("")
    lines.extend(
        [
            "## Recommendation",
            "",
            "- Keep `memory.provider` unchanged until shadow recall passes real-case checks.",
            "- Do not delete `mem0_qdrant`; it may be useful for migration.",
            "- If Hindsight health is unhealthy, fix launchd/wrapper before enabling provider.",
            "",
        ]
    )

    report_path = HERMES_HOME / "reports" / "memory-shadow" / f"{now_stamp()}.md"
    write_report(report_path, "\n".join(lines))
    print(f"{status_line}: memory shadow report written to {report_path}")
    if not health_ok or not port_open:
        print("Hindsight is not healthy; production provider remains unchanged.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
