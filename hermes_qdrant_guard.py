#!/usr/bin/env python3
"""Guard the local Qdrant server used by Hermes long-term memory.

The memory vault is the source of truth. Qdrant is the retrieval index. This
script keeps the local server dependency observable and recoverable without
ever deleting containers, rebuilding storage, or changing images automatically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
MEM0_CONFIG = HERMES_HOME / "mem0.json"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_COLLECTION = "hermes_vault_memories"
CONTAINER_NAME = "hermes-qdrant"
EXPECTED_IMAGE = "ghcr.io/qdrant/qdrant/qdrant:v1.18.2"
EXPECTED_POINTS_MIN = 1
LIMA_HOME = Path.home() / ".colima" / "_lima"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
        f"/bin/{name}",
    ):
        if Path(candidate).exists():
            return candidate
    return name


def run_command(args: list[str], timeout: int = 20) -> tuple[int, str]:
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


def check_detail(result: dict[str, Any], name: str) -> str:
    item = result.get("checks", {}).get(name, {})
    if not isinstance(item, dict):
        return ""
    return str(item.get("detail") or "")


def http_get(url: str, timeout: float = 3.0) -> tuple[bool, Any, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return 200 <= int(resp.status) < 300, payload, ""
    except urllib.error.HTTPError as exc:
        return False, None, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def load_qdrant_config() -> tuple[str, str]:
    if not MEM0_CONFIG.exists():
        return DEFAULT_QDRANT_URL, DEFAULT_COLLECTION
    try:
        data = json.loads(MEM0_CONFIG.read_text(encoding="utf-8"))
        vector = (
            data.get("oss", {})
            .get("vector_store", {})
            .get("config", {})
        )
        if not isinstance(vector, dict):
            return DEFAULT_QDRANT_URL, DEFAULT_COLLECTION
        url = str(vector.get("url") or DEFAULT_QDRANT_URL).rstrip("/")
        collection = str(vector.get("collection_name") or DEFAULT_COLLECTION)
        return url, collection
    except Exception:
        return DEFAULT_QDRANT_URL, DEFAULT_COLLECTION


def empty_result(mode: str) -> dict[str, Any]:
    url, collection = load_qdrant_config()
    return {
        "generated_at": now_iso(),
        "mode": mode,
        "healthy": False,
        "repaired": False,
        "qdrant_url": url,
        "collection": collection,
        "container": CONTAINER_NAME,
        "expected_image": EXPECTED_IMAGE,
        "checks": {},
        "actions": [],
        "errors": [],
    }


def add_check(result: dict[str, Any], name: str, ok: bool, detail: Any = "") -> None:
    result["checks"][name] = {
        "ok": bool(ok),
        "detail": detail,
    }


def docker_inspect(result: dict[str, Any]) -> dict[str, Any]:
    docker = command_path("docker")
    fmt = (
        "{{.State.Status}}|{{.Config.Image}}|{{.HostConfig.RestartPolicy.Name}}|"
        "{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}={{$conf}} {{end}}"
    )
    code, output = run_command([docker, "inspect", CONTAINER_NAME, "--format", fmt], timeout=12)
    if code != 0:
        add_check(result, "docker_container", False, output or f"rc={code}")
        return {}
    parts = output.split("|", 3)
    info = {
        "status": parts[0] if len(parts) > 0 else "",
        "image": parts[1] if len(parts) > 1 else "",
        "restart_policy": parts[2] if len(parts) > 2 else "",
        "ports": parts[3] if len(parts) > 3 else "",
    }
    ok = info["status"] == "running"
    add_check(result, "docker_container", ok, info)
    return info


def check_colima(result: dict[str, Any]) -> bool:
    colima = command_path("colima")
    if not shutil.which("colima") and not Path(colima).exists():
        add_check(result, "colima", False, "colima command not found")
        return False
    code, output = run_command([colima, "status"], timeout=12)
    running = code == 0 and "colima is running" in output.lower()
    detail = output or f"rc={code}"
    if not running:
        list_code, list_output = run_command([colima, "list"], timeout=12)
        if list_output:
            detail = f"{detail}\ncolima list rc={list_code}:\n{list_output}"
    add_check(result, "colima", running, detail)
    return running


def check_docker(result: dict[str, Any]) -> bool:
    docker = command_path("docker")
    if not shutil.which("docker") and not Path(docker).exists():
        add_check(result, "docker", False, "docker command not found")
        return False
    code, output = run_command([docker, "info", "--format", "{{.ServerVersion}}"], timeout=12)
    ok = code == 0 and bool(output.strip())
    add_check(result, "docker", ok, output or f"rc={code}")
    return ok


def colima_list_running() -> bool:
    colima = command_path("colima")
    code, output = run_command([colima, "list"], timeout=12)
    if code != 0:
        return False
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "default" and parts[1].lower() == "running":
            return True
    return False


def force_stop_lima_colima(result: dict[str, Any]) -> None:
    limactl = command_path("limactl")
    code, output = run_command(
        ["env", f"LIMA_HOME={LIMA_HOME}", limactl, "stop", "--force", "colima"],
        timeout=45,
    )
    result["actions"].append({"action": "limactl stop --force colima", "rc": code, "output": output})


def restart_colima(result: dict[str, Any]) -> None:
    colima = command_path("colima")
    stop_code, stop_output = run_command([colima, "stop"], timeout=75)
    result["actions"].append({"action": "colima stop", "rc": stop_code, "output": stop_output})
    if stop_code != 0 and ("timeoutexpired" in stop_output.lower() or colima_list_running()):
        force_stop_lima_colima(result)
    start_code, start_output = run_command([colima, "start"], timeout=240)
    result["actions"].append({"action": "colima start", "rc": start_code, "output": start_output})


def wait_for_docker(result: dict[str, Any], *, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if check_docker(result):
            return True
        time.sleep(3)
    return check_docker(result)


def check_qdrant_http(result: dict[str, Any]) -> tuple[bool, int]:
    url = str(result["qdrant_url"]).rstrip("/")
    collection = str(result["collection"])
    health_ok, health_payload, health_error = http_get(f"{url}/healthz", timeout=3)
    add_check(result, "qdrant_healthz", health_ok, health_payload if health_ok else health_error)

    collection_ok, payload, error = http_get(f"{url}/collections/{collection}", timeout=5)
    points = 0
    collection_detail: dict[str, Any] = {"points_count": 0}
    if collection_ok and isinstance(payload, dict):
        body = payload.get("result", {})
        if isinstance(body, dict):
            try:
                points = int(body.get("points_count") or 0)
            except Exception:
                points = 0
            collection_detail = {
                "status": body.get("status", ""),
                "optimizer_status": body.get("optimizer_status", ""),
                "points_count": points,
                "indexed_vectors_count": body.get("indexed_vectors_count", 0),
                "segments_count": body.get("segments_count", 0),
            }
    points_ok = collection_ok and points >= EXPECTED_POINTS_MIN
    add_check(
        result,
        "qdrant_collection",
        points_ok,
        collection_detail if collection_ok else error,
    )
    return health_ok and points_ok, points


def check() -> dict[str, Any]:
    result = empty_result("check")
    colima_ok = check_colima(result)
    docker_ok = check_docker(result)
    info = docker_inspect(result) if docker_ok else {}
    http_ok, points = check_qdrant_http(result)
    result["points_count"] = points
    result["healthy"] = bool(colima_ok and docker_ok and info.get("status") == "running" and http_ok)
    return result


def repair() -> dict[str, Any]:
    result = empty_result("repair")
    colima_ok = check_colima(result)
    docker = command_path("docker")
    if not colima_ok:
        restart_colima(result)

    docker_ok = wait_for_docker(result)
    if not docker_ok and colima_ok and colima_list_running():
        restart_colima(result)
        docker_ok = wait_for_docker(result)
    if not docker_ok:
        result["errors"].append("docker daemon is not reachable after colima start")
        return result

    info = docker_inspect(result)
    if not info:
        result["errors"].append(f"container {CONTAINER_NAME} does not exist; refusing to create it automatically")
        return result

    if info.get("status") != "running":
        code, output = run_command([docker, "start", CONTAINER_NAME], timeout=60)
        result["actions"].append({"action": f"docker start {CONTAINER_NAME}", "rc": code, "output": output})
        time.sleep(3)

    http_ok, points = check_qdrant_http(result)
    if not http_ok:
        code, output = run_command([docker, "restart", CONTAINER_NAME], timeout=90)
        result["actions"].append({"action": f"docker restart {CONTAINER_NAME}", "rc": code, "output": output})
        time.sleep(5)
        http_ok, points = check_qdrant_http(result)

    result["points_count"] = points
    refreshed = docker_inspect(result)
    result["healthy"] = bool(refreshed.get("status") == "running" and http_ok)
    result["repaired"] = bool(result["actions"] and result["healthy"])
    if not result["healthy"] and not result["errors"]:
        result["errors"].append("qdrant server did not become healthy")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or repair Hermes Qdrant memory index service.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check service health and print JSON.")
    mode.add_argument("--repair", action="store_true", help="Apply low-risk service recovery and print JSON.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    result = repair() if args.repair else check()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = "healthy" if result["healthy"] else "unhealthy"
        action_count = len(result.get("actions", []))
        print(f"Hermes Qdrant guard: {status}, actions={action_count}, points={result.get('points_count', 0)}")
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
