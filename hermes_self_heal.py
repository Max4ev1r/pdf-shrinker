#!/usr/bin/env python3
"""Hermes daily self-heal loop.

This script turns the shadow/review/action reports into an operational loop:
- observe local services, cron jobs, scripts, disk, and memory config
- auto-repair only low-risk local problems
- keep durable reports for auditing
- stay silent when nothing needs attention

It intentionally does not write USER.md, MEMORY.md, SOUL.md, skills, medical
facts, product conclusions, or user preference memory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
SCRIPTS_DIR = HERMES_HOME / "scripts"
REPORT_DIR = HERMES_HOME / "reports" / "self-heal"
STATE_FILE = REPORT_DIR / "state.json"
KNOWN_ISSUES_FILE = REPORT_DIR / "known-issues.json"
CRON_JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
CONFIG_FILE = HERMES_HOME / "config.yaml"
HERMES_PYTHON = HERMES_HOME / "hermes-agent" / ".venv" / "bin" / "python"
EXPERT_PYTHON = HERMES_HOME / "mcp-servers" / "expert-tools" / ".venv" / "bin" / "python"
ASTROLOGY_SMOKE_SCRIPT = (
    HERMES_HOME
    / "skills"
    / "specialist"
    / "chinese-astrology-expert"
    / "scripts"
    / "test_routing_regression.py"
)
QDRANT_GUARD_SCRIPT = SCRIPTS_DIR / "hermes_qdrant_guard.py"
TELEGRAM_GUARD_SCRIPT = SCRIPTS_DIR / "hermes_telegram_guard.py"
HINDSIGHT_LABEL = "ai.hermes.hindsight"
HINDSIGHT_HEALTH_URL = "http://127.0.0.1:8100/health"

REPORT_STALE_HOURS = 20
REPORT_KEEP_DAYS = 60
REPORT_PRUNE_MIN_BYTES = 50 * 1024 * 1024
DISK_WARN_BYTES = 15 * 1024 * 1024 * 1024
DISK_CRITICAL_BYTES = 5 * 1024 * 1024 * 1024
LOG_WARN_BYTES = 750 * 1024 * 1024
HINDSIGHT_RSS_WARN_KB = 1_500_000
CRON_ESCALATE_COUNT = 3


@dataclass
class Event:
    level: str
    area: str
    status: str
    message: str
    action: str = ""
    notify: bool = False


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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def http_json(url: str, timeout: float = 5.0) -> tuple[bool, Any, str]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return True, json.loads(raw), ""
        except json.JSONDecodeError:
            return True, raw, ""
    except urllib.error.HTTPError as exc:
        return False, None, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
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


def latest_file(path: Path, pattern: str = "20*.md") -> Path | None:
    files = sorted(path.glob(pattern))
    return files[-1] if files else None


def file_age_hours(path: Path) -> float:
    return max(0.0, (time.time() - path.stat().st_mtime) / 3600.0)


def fingerprint(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha1(clean[:600].encode("utf-8")).hexdigest()[:12]


def load_state() -> dict[str, Any]:
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return {}
    state.setdefault("cron_failures", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now().isoformat()
    write_json(STATE_FILE, state)


def add(events: list[Event], level: str, area: str, status: str, message: str, action: str = "", notify: bool = False) -> None:
    events.append(Event(level=level, area=area, status=status, message=message, action=action, notify=notify))


def check_hindsight(events: list[Event], *, dry_run: bool) -> None:
    port_ok = tcp_open("127.0.0.1", 8100)
    health_ok, payload, error = http_json(HINDSIGHT_HEALTH_URL)
    healthy = port_ok and health_ok and isinstance(payload, dict) and payload.get("status") == "healthy"

    launch_code, launch_out = run_command(["launchctl", "list", HINDSIGHT_LABEL], timeout=8)
    pid_match = re.search(r'"PID"\s*=\s*(\d+);', launch_out)
    pid = pid_match.group(1) if pid_match else ""
    rss_kb: int | None = None
    if pid:
        ps_code, ps_out = run_command(["ps", "-o", "rss=", "-p", pid], timeout=5)
        if ps_code == 0:
            try:
                rss_kb = int(ps_out.strip().splitlines()[-1])
            except Exception:
                rss_kb = None

    if healthy:
        message = "healthy"
        if rss_kb is not None:
            message += f", rss={fmt_size(rss_kb * 1024)}"
        add(events, "ok", "hindsight", "healthy", message)
        if rss_kb is not None and rss_kb > HINDSIGHT_RSS_WARN_KB:
            add(
                events,
                "warning",
                "hindsight",
                "rss_high",
                f"Hindsight RSS is {fmt_size(rss_kb * 1024)}, above warning threshold.",
                notify=True,
            )
        return

    detail = error or str(payload) or "health check failed"
    if launch_code != 0:
        detail += f"; launchctl={launch_out}"
    if dry_run:
        add(events, "action", "hindsight", "would_restart", detail, action="dry-run launchctl kickstart", notify=True)
        return

    uid = str(os.getuid())
    restart_code, restart_out = run_command(["launchctl", "kickstart", "-k", f"gui/{uid}/{HINDSIGHT_LABEL}"], timeout=20)
    time.sleep(3)
    post_ok, post_payload, post_error = http_json(HINDSIGHT_HEALTH_URL)
    fixed = post_ok and isinstance(post_payload, dict) and post_payload.get("status") == "healthy"
    if fixed:
        add(
            events,
            "action",
            "hindsight",
            "restarted",
            f"Hindsight was unhealthy ({detail}); restart succeeded.",
            action=f"launchctl kickstart -k gui/{uid}/{HINDSIGHT_LABEL}",
            notify=True,
        )
    else:
        add(
            events,
            "critical",
            "hindsight",
            "restart_failed",
            f"Hindsight unhealthy ({detail}); restart rc={restart_code}, out={restart_out}, post={post_error or post_payload}",
            action=f"launchctl kickstart -k gui/{uid}/{HINDSIGHT_LABEL}",
            notify=True,
        )


def run_qdrant_guard(mode: str) -> tuple[int, dict[str, Any], str]:
    if not QDRANT_GUARD_SCRIPT.exists():
        return 127, {}, f"{QDRANT_GUARD_SCRIPT} missing"
    runner = HERMES_PYTHON if HERMES_PYTHON.exists() else Path(sys.executable)
    flag = "--repair" if mode == "repair" else "--check"
    code, output = run_command([str(runner), str(QDRANT_GUARD_SCRIPT), flag, "--json"], timeout=240)
    try:
        payload = json.loads(output)
    except Exception:
        payload = {}
    return code, payload if isinstance(payload, dict) else {}, output


def summarize_qdrant_guard(payload: dict[str, Any], fallback: str = "") -> str:
    if not payload:
        return fallback or "no guard payload"
    url = payload.get("qdrant_url") or "unknown URL"
    collection = payload.get("collection") or "unknown collection"
    points = payload.get("points_count", 0)
    errors = payload.get("errors") or []
    actions = payload.get("actions") or []
    detail = f"{url} collection={collection} points={points}"
    if actions:
        detail += f", actions={len(actions)}"
    if errors:
        detail += f", errors={errors}"
    return detail


def check_qdrant_memory_infra(events: list[Event], *, dry_run: bool) -> bool:
    code, payload, output = run_qdrant_guard("check")
    if code == 0 and payload.get("healthy"):
        add(events, "ok", "memory-infra", "qdrant_healthy", summarize_qdrant_guard(payload))
        return True

    detail = summarize_qdrant_guard(payload, output)
    if dry_run:
        add(
            events,
            "action",
            "memory-infra",
            "would_repair_qdrant",
            detail,
            action=str(QDRANT_GUARD_SCRIPT),
            notify=True,
        )
        return False

    repair_code, repair_payload, repair_output = run_qdrant_guard("repair")
    repair_detail = summarize_qdrant_guard(repair_payload, repair_output)
    if repair_code == 0 and repair_payload.get("healthy"):
        add(
            events,
            "action",
            "memory-infra",
            "qdrant_repaired",
            f"Qdrant was unhealthy ({detail}); repair succeeded: {repair_detail}",
            action=str(QDRANT_GUARD_SCRIPT),
            notify=True,
        )
        return True

    add(
        events,
        "critical",
        "memory-infra",
        "qdrant_unhealthy",
        f"Qdrant is unhealthy and repair failed: {repair_detail}",
        action=str(QDRANT_GUARD_SCRIPT),
        notify=True,
    )
    return False


def run_telegram_guard() -> tuple[int, dict[str, Any], str]:
    if not TELEGRAM_GUARD_SCRIPT.exists():
        return 127, {}, f"{TELEGRAM_GUARD_SCRIPT} missing"
    runner = HERMES_PYTHON if HERMES_PYTHON.exists() else Path(sys.executable)
    code, output = run_command([str(runner), str(TELEGRAM_GUARD_SCRIPT), "--check", "--json"], timeout=45)
    try:
        payload = json.loads(output)
    except Exception:
        payload = {}
    return code, payload if isinstance(payload, dict) else {}, output


def summarize_telegram_guard(payload: dict[str, Any], fallback: str = "") -> str:
    if not payload:
        return fallback or "no telegram guard payload"
    status = payload.get("status", "unknown")
    proxy = payload.get("proxy", {}) if isinstance(payload.get("proxy"), dict) else {}
    get_me = payload.get("get_me", {}) if isinstance(payload.get("get_me"), dict) else {}
    log = payload.get("gateway_log", {}) if isinstance(payload.get("gateway_log"), dict) else {}
    parts = [
        f"status={status}",
        f"token_present={bool(payload.get('token_present'))}",
    ]
    if payload.get("proxy_url"):
        parts.append(
            f"proxy={proxy.get('host', '')}:{proxy.get('port', '')} reachable={proxy.get('reachable')}"
        )
    if get_me:
        parts.append(f"getMe={get_me.get('ok')} status={get_me.get('status', 0)}")
        if get_me.get("error"):
            parts.append(f"error={get_me.get('error')}")
    if log:
        parts.append(
            f"log_errors={log.get('error_count', 0)} log_success={log.get('success_count', 0)}"
        )
        if log.get("last_error_at"):
            parts.append(f"last_error_at={log.get('last_error_at')}")
    return ", ".join(parts)


def check_telegram_gateway(events: list[Event]) -> None:
    code, payload, output = run_telegram_guard()
    detail = summarize_telegram_guard(payload, output)
    status = str(payload.get("status", "unknown")) if payload else "guard_failed"
    if code == 0 and status in {"healthy", "not_configured"}:
        add(events, "ok", "gateway-telegram", f"telegram_{status}", detail)
        return
    if status == "flapping":
        add(events, "warning", "gateway-telegram", "telegram_flapping", detail, notify=False)
        return
    if status == "proxy_down":
        add(events, "critical", "gateway-telegram", "telegram_proxy_down", detail, notify=True)
        return
    if status == "token_missing":
        add(events, "critical", "gateway-telegram", "telegram_token_missing", detail, notify=True)
        return
    if status == "api_unreachable":
        add(events, "warning", "gateway-telegram", "telegram_api_unreachable", detail, notify=True)
        return
    add(events, "warning", "gateway-telegram", "telegram_guard_failed", detail, notify=True)


def memory_provider() -> str:
    if not CONFIG_FILE.exists():
        return ""
    lines = CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
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
            return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""


def latest_shadow_holds_memory() -> tuple[bool, str]:
    latest = latest_file(HERMES_HOME / "reports" / "memory-shadow")
    if not latest:
        return False, ""
    text = latest.read_text(encoding="utf-8", errors="replace")
    holds = "Keep `memory.provider` unchanged" in text or "External memory backend: HOLD" in text
    return holds, str(latest)


def latest_memory_audit_allows_provider(provider: str) -> tuple[bool, str, str]:
    latest = latest_file(HERMES_HOME / "reports" / "memory-audit", pattern="20*.json")
    if not latest:
        return False, "", "no memory-audit JSON report found"
    payload = read_json(latest, {})
    mem0 = payload.get("mem0", {}) if isinstance(payload, dict) else {}
    sync = payload.get("mem0_sync", {}) if isinstance(payload, dict) else {}
    if provider == "vault":
        if not (HERMES_HOME / "memory-vault" / "vault.sqlite3").exists():
            return False, str(latest), "vault authority file is missing"
        if mem0.get("active_memory_provider") != "vault":
            return False, str(latest), "latest memory-audit did not observe vault as active provider"
        database = payload.get("vault_database", {}) if isinstance(payload, dict) else {}
        if not isinstance(database, dict) or not database.get("healthy"):
            return False, str(latest), "vault database integrity, event chain, or index outbox is unhealthy"
        return True, str(latest), "vault authority gate passed; derived index status is tracked separately"
    if provider != "mem0":
        return False, str(latest), f"provider {provider!r} is not approved by the memory-audit gate"
    if mem0.get("active_memory_provider") != "mem0":
        return False, str(latest), "latest memory-audit did not observe mem0 as active provider"
    if not mem0.get("ready_for_production"):
        return False, str(latest), "mem0 is not ready_for_production"
    if sync.get("errors"):
        return False, str(latest), "mem0 sync has errors"
    return True, str(latest), "mem0 production gate passed"


def blank_memory_provider(events: list[Event], *, dry_run: bool) -> None:
    lines = CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    in_memory = False
    memory_indent = 0
    changed = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "memory:":
            in_memory = True
            memory_indent = indent
            out.append(line)
            continue
        if in_memory and indent <= memory_indent and stripped and not line.startswith(" "):
            in_memory = False
        if in_memory and stripped.startswith("provider:"):
            out.append(" " * indent + 'provider: ""')
            changed = True
            continue
        out.append(line)
    if not changed:
        return
    if dry_run:
        add(events, "action", "memory", "would_disable_provider", "config.yaml memory.provider would be blanked.", notify=True)
        return
    backup = CONFIG_FILE.with_name(f"config.yaml.selfheal-{stamp()}.bak")
    shutil.copy2(CONFIG_FILE, backup)
    CONFIG_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    add(
        events,
        "action",
        "memory",
        "provider_disabled",
        f"Disabled external memory provider because latest shadow gate still holds production memory. Backup: {backup}",
        action="set config.yaml memory.provider to blank",
        notify=True,
    )


def check_memory_config(events: list[Event], *, dry_run: bool, memory_infra_ok: bool = True) -> None:
    provider = memory_provider()
    if not provider:
        add(events, "ok", "memory", "built_in_only", "Production memory provider is built-in only.")
        return
    if not memory_infra_ok and provider != "vault":
        add(
            events,
            "critical",
            "memory",
            "provider_blocked_by_infra",
            f"memory.provider is {provider!r}, but Qdrant/mem0 infrastructure is unhealthy. Leaving config unchanged.",
            notify=True,
        )
        return
    allowed, audit_path, detail = latest_memory_audit_allows_provider(provider)
    if allowed:
        add(events, "ok", "memory", "external_provider_approved", f"memory.provider is {provider!r}; {detail}: {audit_path}")
        return
    holds, shadow = latest_shadow_holds_memory()
    if holds:
        add(
            events,
            "warning",
            "memory",
            "provider_shadow_hold",
            f"memory.provider remains {provider!r}; shadow validation is holding: {shadow}",
            notify=True,
        )
        return
    add(
        events,
        "critical",
        "memory",
        "external_provider_enabled",
        f"memory.provider is {provider!r}, but no passing memory-audit approval was found ({detail}). Latest audit: {audit_path or 'none'}; latest shadow: {shadow or 'none'}",
        notify=True,
    )


def config_section_value(section: str, key: str) -> str:
    if not CONFIG_FILE.exists():
        return ""
    lines = CONFIG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    in_section = False
    section_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == f"{section}:":
            in_section = True
            section_indent = indent
            continue
        if in_section and indent <= section_indent:
            break
        if in_section and stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""


def enforce_controlled_write_gates(events: list[Event], *, dry_run: bool) -> None:
    missing = [
        section
        for section in ("memory", "skills")
        if config_section_value(section, "write_approval").lower() not in {"true", "yes", "on", "1"}
    ]
    if not missing:
        add(events, "ok", "learning-policy", "write_gates_enabled", "Memory and skill background writes require approval.")
        return
    if dry_run:
        add(
            events,
            "action",
            "learning-policy",
            "would_restore_write_gates",
            f"write_approval disabled for: {', '.join(missing)}",
            notify=True,
        )
        return

    original = CONFIG_FILE.read_text(encoding="utf-8")
    backup = CONFIG_FILE.with_name(f"config.yaml.phase3b-{stamp()}.bak")
    shutil.copy2(CONFIG_FILE, backup)
    lines = original.splitlines()
    out: list[str] = []
    section = ""
    section_indent = 0
    changed: set[str] = set()
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped in {"memory:", "skills:"}:
            section = stripped[:-1]
            section_indent = indent
            out.append(line)
            continue
        if section and stripped and indent <= section_indent:
            section = ""
        if section in missing and stripped.startswith("write_approval:"):
            out.append(" " * indent + "write_approval: true")
            changed.add(section)
            continue
        out.append(line)
    CONFIG_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")

    verified = all(
        config_section_value(section, "write_approval").lower() in {"true", "yes", "on", "1"}
        for section in ("memory", "skills")
    )
    if verified and changed == set(missing):
        add(
            events,
            "action",
            "learning-policy",
            "write_gates_restored",
            f"Restored approval gates for {', '.join(missing)} and verified config. Backup: {backup}",
            notify=True,
        )
        return

    shutil.copy2(backup, CONFIG_FILE)
    add(
        events,
        "critical",
        "learning-policy",
        "write_gate_restore_failed",
        f"Could not safely restore approval gates for {', '.join(missing)}; backup restored.",
        notify=True,
    )


def check_builtin_memory_capacity(events: list[Event]) -> None:
    targets = [
        ("user", HERMES_HOME / "memories" / "USER.md", config_section_value("memory", "user_char_limit")),
        ("memory", HERMES_HOME / "memories" / "MEMORY.md", config_section_value("memory", "memory_char_limit")),
    ]
    for label, path, raw_limit in targets:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            add(events, "warning", "memory-capacity", f"{label}_limit_invalid", f"Invalid configured limit: {raw_limit!r}.")
            continue
        content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        used = len(content)
        pct = used / limit if limit > 0 else 1.0
        message = f"{path.name} uses {used}/{limit} chars ({pct:.0%})."
        if pct > 1:
            add(events, "critical", "memory-capacity", f"{label}_over_limit", message, notify=True)
        elif pct >= 0.9:
            add(events, "warning", "memory-capacity", f"{label}_near_limit", message)
        else:
            add(events, "ok", "memory-capacity", f"{label}_capacity_ok", message)


def check_astrology_semantics(events: list[Event]) -> None:
    if not EXPERT_PYTHON.exists() or not ASTROLOGY_SMOKE_SCRIPT.exists():
        add(
            events,
            "critical",
            "astrology",
            "smoke_test_missing",
            f"Missing runtime or smoke test: {EXPERT_PYTHON}, {ASTROLOGY_SMOKE_SCRIPT}",
            notify=True,
        )
        return
    code, output = run_command(
        [str(EXPERT_PYTHON), str(ASTROLOGY_SMOKE_SCRIPT)],
        timeout=45,
    )
    if code == 0:
        add(events, "ok", "astrology", "semantic_smoke_passed", output)
        return
    add(
        events,
        "critical",
        "astrology",
        "semantic_smoke_failed",
        output or f"exit code {code}",
        notify=True,
    )


def maybe_run_report_script(events: list[Event], report_subdir: str, script: str, *, dry_run: bool) -> None:
    report_dir = HERMES_HOME / "reports" / report_subdir
    latest = latest_file(report_dir)
    if latest and file_age_hours(latest) <= REPORT_STALE_HOURS:
        add(events, "ok", report_subdir, "fresh", f"Latest report is {latest.name}, age={file_age_hours(latest):.1f}h.")
        return
    reason = "missing" if latest is None else f"stale age={file_age_hours(latest):.1f}h"
    if dry_run:
        add(events, "action", report_subdir, "would_regenerate", reason, action=script, notify=True)
        return
    script_path = SCRIPTS_DIR / script
    if not script_path.exists():
        add(events, "critical", report_subdir, "script_missing", f"{script_path} missing.", notify=True)
        return
    before_path = latest
    before_mtime = latest.stat().st_mtime if latest else 0.0
    runner = HERMES_PYTHON if HERMES_PYTHON.exists() else Path(sys.executable)
    code, output = run_command([str(runner), str(script_path)], timeout=180)
    if code != 0:
        add(events, "critical", report_subdir, "regenerate_failed", f"{reason}; rc={code}; output={output}", notify=True)
        return

    regenerated = latest_file(report_dir)
    produced_new_report = bool(
        regenerated
        and file_age_hours(regenerated) <= 0.25
        and (
            before_path is None
            or regenerated != before_path
            or regenerated.stat().st_mtime > before_mtime
        )
    )
    if not produced_new_report:
        add(
            events,
            "critical",
            report_subdir,
            "regenerate_unverified",
            f"{reason}; script exited 0 but no new fresh report appeared; output={output}",
            action=script,
            notify=True,
        )
        return
    add(
        events,
        "action",
        report_subdir,
        "regenerated_verified",
        f"{reason}; generated {regenerated.name} and verified freshness.",
        action=script,
        notify=False,
    )


def check_reports(events: list[Event], *, dry_run: bool) -> None:
    maybe_run_report_script(events, "memory-shadow", "hindsight_shadow_check.py", dry_run=dry_run)
    maybe_run_report_script(events, "memory-audit", "memory_vault.py", dry_run=dry_run)
    maybe_run_report_script(events, "memory-retrieval", "memory_retrieval_eval.py", dry_run=dry_run)
    maybe_run_report_script(events, "memory-governance", "memory_governor.py", dry_run=dry_run)
    maybe_run_report_script(events, "learning-review", "hermes_learning_review.py", dry_run=dry_run)
    maybe_run_report_script(events, "learning-actions", "hermes_learning_actions.py", dry_run=dry_run)
    maybe_run_report_script(events, "controlled-learning", "hermes_controlled_learning.py", dry_run=dry_run)


def check_memory_governance_report(events: list[Event], state: dict[str, Any]) -> None:
    latest = latest_file(HERMES_HOME / "reports" / "memory-governance", pattern="20*.json")
    if not latest:
        add(events, "warning", "memory-governance", "missing", "No memory-governance JSON report found.", notify=True)
        return
    payload = read_json(latest, {})
    if not isinstance(payload, dict):
        add(events, "warning", "memory-governance", "unreadable", f"Cannot parse {latest}.", notify=True)
        return
    errors = payload.get("errors", [])
    if errors:
        add(events, "critical", "memory-governance", "errors", f"{len(errors)} governance error(s) in {latest}.", notify=True)
        return
    review_count = len(payload.get("needs_user_review", []) or [])
    merge_count = len(payload.get("merge_candidates", []) or [])
    if review_count or merge_count:
        candidate_ids = sorted(
            str(item.get("id", ""))
            for section in ("needs_user_review", "merge_candidates")
            for item in (payload.get(section, []) or [])
            if isinstance(item, dict) and item.get("id")
        )
        fingerprint = hashlib.sha256("\n".join(candidate_ids).encode("utf-8")).hexdigest()[:16]
        review_state = state.setdefault("memory_governance_review", {})
        previous = str(review_state.get("fingerprint", ""))
        if not previous:
            known = (state.get("known_issues", {}) or {}).get("memory-governance:pending_review", {})
            if isinstance(known, dict) and known.get("state") == "active":
                previous = fingerprint
        changed = fingerprint != previous
        review_state.update({
            "fingerprint": fingerprint,
            "candidate_ids": candidate_ids,
            "count": review_count + merge_count,
            "updated_at": now().isoformat(),
        })
        add(
            events,
            "warning" if changed else "info",
            "memory-governance",
            "pending_review" if changed else "pending_review_unchanged",
            f"{review_count} high-risk/review candidate(s), {merge_count} merge candidate(s) need confirmation. See the local memory-governance report.",
            notify=changed,
        )
        return
    state.pop("memory_governance_review", None)
    add(events, "ok", "memory-governance", "clean", f"Latest governance report is {latest.name}.")


def prune_generated_reports(events: list[Event], *, dry_run: bool) -> None:
    cutoff = time.time() - REPORT_KEEP_DAYS * 86400
    total_deleted = 0
    total_bytes = 0
    roots = [
        HERMES_HOME / "reports" / "memory-shadow",
        HERMES_HOME / "reports" / "memory-audit",
        HERMES_HOME / "reports" / "memory-retrieval",
        HERMES_HOME / "reports" / "memory-governance",
        HERMES_HOME / "reports" / "learning-review",
        HERMES_HOME / "reports" / "learning-actions",
        HERMES_HOME / "reports" / "controlled-learning",
        HERMES_HOME / "reports" / "self-heal",
    ]
    for root in roots:
        size = directory_size(root)
        if size < REPORT_PRUNE_MIN_BYTES:
            continue
        for item in root.glob("*"):
            if not item.is_file():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            total_deleted += 1
            total_bytes += stat.st_size
            if not dry_run:
                try:
                    item.unlink()
                except OSError as exc:
                    add(events, "warning", "retention", "delete_failed", f"{item}: {exc}")
    if total_deleted:
        action = "dry-run prune" if dry_run else "pruned old generated reports"
        add(
            events,
            "action",
            "retention",
            "pruned",
            f"{total_deleted} generated report files older than {REPORT_KEEP_DAYS}d, {fmt_size(total_bytes)}.",
            action=action,
            notify=total_bytes >= 50 * 1024 * 1024,
        )
    else:
        add(events, "ok", "retention", "no_prune_needed", "Generated report retention is within thresholds.")


def load_jobs() -> list[dict[str, Any]]:
    data = read_json(CRON_JOBS_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data["jobs"]
    return []


def script_path(script: str) -> Path:
    path = Path(script)
    if path.is_absolute():
        return path
    return SCRIPTS_DIR / script


def check_script_health(events: list[Event], jobs: list[dict[str, Any]]) -> None:
    seen: set[Path] = set()
    for job in jobs:
        if not job.get("enabled", True):
            continue
        script = (job.get("script") or "").strip()
        if not script:
            continue
        path = script_path(script)
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            add(events, "critical", "scripts", "missing", f"Active cron script missing: {path}", notify=True)
            continue
        if path.suffix == ".py":
            code, output = run_command([str(HERMES_PYTHON), "-m", "py_compile", str(path)], timeout=30)
            if code != 0:
                add(events, "critical", "scripts", "syntax_error", f"{path}: {output}", notify=True)
            else:
                add(events, "ok", "scripts", "py_compile_ok", str(path))
        elif path.suffix in {".sh", ".bash"}:
            code, output = run_command(["bash", "-n", str(path)], timeout=15)
            if code != 0:
                add(events, "critical", "scripts", "syntax_error", f"{path}: {output}", notify=True)
            else:
                add(events, "ok", "scripts", "bash_syntax_ok", str(path))


def cron_failure_text(job: dict[str, Any]) -> str:
    parts = []
    if job.get("last_status") and job.get("last_status") != "ok":
        parts.append(f"status={job.get('last_status')}")
    if job.get("last_error"):
        parts.append(str(job.get("last_error")))
    if job.get("last_delivery_error"):
        parts.append(str(job.get("last_delivery_error")))
    return "; ".join(parts)


def classify_cron_failure(job: dict[str, Any], text: str) -> tuple[str, bool]:
    lower = text.lower()
    delivery_error = str(job.get("last_delivery_error") or "").lower()
    if delivery_error:
        if "rate limited" in delivery_error or "cooldown active" in delivery_error:
            return "delivery_rate_limited", False
        if "http 429" in delivery_error or "quota exhausted" in delivery_error:
            return "delivery_provider_quota", False
        return "delivery_failure", False
    if "rate limited" in lower or "cooldown active" in lower:
        return "provider_rate_limited", False
    if "http 429" in lower or "quota exhausted" in lower:
        return "provider_quota", False
    if "timeout" in lower:
        return "timeout", False
    if "script missing" in lower or "no such file" in lower:
        return "missing_script", True
    return "job_failure", False


def check_cron_jobs(events: list[Event], jobs: list[dict[str, Any]], state: dict[str, Any]) -> None:
    failures = state.setdefault("cron_failures", {})
    seen_failure_ids: set[str] = set()
    active_jobs = [job for job in jobs if job.get("enabled", True)]
    add(events, "ok", "cron", "active_jobs", f"{len(active_jobs)} active jobs loaded.")

    for job in active_jobs:
        text = cron_failure_text(job)
        if not text:
            failures.pop(str(job.get("id")), None)
            continue
        job_id = str(job.get("id") or "unknown")
        seen_failure_ids.add(job_id)
        fp = fingerprint(text)
        previous = failures.get(job_id, {})
        run_at = str(job.get("last_run_at") or "")
        same_failure = previous.get("fingerprint") == fp
        same_run = same_failure and "last_run_at" in previous and previous.get("last_run_at") == run_at
        if same_failure and same_run:
            count = int(previous.get("count", 1))
        elif same_failure:
            count = int(previous.get("count", 0)) + 1
        else:
            count = 1
        failures[job_id] = {
            "fingerprint": fp,
            "count": count,
            "name": job.get("name") or job_id,
            "last_seen": now().isoformat(),
            "last_run_at": run_at,
            "text": text,
        }
        kind, immediate = classify_cron_failure(job, text)
        notify = immediate or count >= CRON_ESCALATE_COUNT
        level = "critical" if immediate else ("warning" if notify else "info")
        add(
            events,
            level,
            "cron",
            kind,
            f"{job.get('name') or job_id} ({job_id}) failure count={count}: {text}",
            notify=notify,
        )

    for job_id in list(failures.keys()):
        if job_id not in seen_failure_ids and not any(str(job.get("id")) == job_id for job in active_jobs):
            failures.pop(job_id, None)


def check_cron_prompt_memory_writes(events: list[Event], jobs: list[dict[str, Any]]) -> None:
    """Flag scheduled prompts that ask the model to edit durable memory files."""
    memory_file_re = re.compile(r"\b(?:MEMORY|USER|SOUL)\.md\b|~/.hermes/(?:memories|memory)/", re.IGNORECASE)
    write_verb_re = re.compile(
        r"追加|写入|编辑|修改|更新到|保存到|改写|append|write|edit|modify|update",
        re.IGNORECASE,
    )
    explicit_safe_re = re.compile(r"不要直接编辑|不要自行写文件|只读|read[- ]?only", re.IGNORECASE)
    risky: list[str] = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        prompt = str(job.get("prompt") or "")
        if not prompt:
            continue
        if memory_file_re.search(prompt) and write_verb_re.search(prompt) and not explicit_safe_re.search(prompt):
            risky.append(f"{job.get('name') or job.get('id')} ({job.get('id')})")

    if risky:
        add(
            events,
            "warning",
            "cron-policy",
            "direct_memory_write_prompt",
            "Active cron prompt(s) ask the model to edit durable memory files directly: "
            + "; ".join(risky[:5])
            + ("; ..." if len(risky) > 5 else ""),
            notify=True,
        )
    else:
        add(events, "ok", "cron-policy", "no_direct_memory_write_prompts", "No active cron prompts directly edit durable memory files.")


def check_capacity(events: list[Event]) -> None:
    disk = shutil.disk_usage(str(HERMES_HOME))
    free = disk.free
    if free < DISK_CRITICAL_BYTES:
        add(events, "critical", "capacity", "disk_critical", f"Hermes volume free space is {fmt_size(free)}.", notify=True)
    elif free < DISK_WARN_BYTES:
        add(events, "warning", "capacity", "disk_low", f"Hermes volume free space is {fmt_size(free)}.", notify=True)
    else:
        add(events, "ok", "capacity", "disk_ok", f"Hermes volume free space is {fmt_size(free)}.")
    log_size = directory_size(HERMES_HOME / "logs")
    if log_size > LOG_WARN_BYTES:
        add(events, "warning", "capacity", "logs_large", f"logs size is {fmt_size(log_size)}.", notify=True)
    else:
        add(events, "ok", "capacity", "logs_ok", f"logs size is {fmt_size(log_size)}.")


def issue_identity(event: Event) -> tuple[str, str]:
    if event.area == "hindsight" and event.status in {"restarted", "restart_failed", "would_restart"}:
        return "hindsight:unhealthy", "Restart Hindsight and verify /health reports healthy."
    if event.area == "memory-infra" and event.status in {
        "qdrant_repaired",
        "qdrant_unhealthy",
        "would_repair_qdrant",
    }:
        return "memory-infra:qdrant", "Start Colima/Docker, start the hermes-qdrant container, and verify Qdrant /healthz plus collection points."
    if event.area == "gateway-telegram":
        return "gateway-telegram:connectivity", "Verify TELEGRAM_BOT_TOKEN, local proxy reachability, Telegram getMe, and gateway reconnect logs before changing gateway behavior."
    if event.area == "memory" and event.status == "provider_blocked_by_infra":
        return "memory:external_provider_infra", "Repair Qdrant/mem0 infrastructure before changing memory.provider."
    if event.area == "memory" and event.status in {
        "provider_disabled",
        "external_provider_enabled",
        "would_disable_provider",
    }:
        return "memory:external_provider_drift", "Restore built-in-only memory while the shadow gate is HOLD."
    if event.status in {
        "regenerated_verified",
        "regenerate_failed",
        "regenerate_unverified",
        "would_regenerate",
    }:
        return f"report:{event.area}:stale", "Regenerate the report and verify a new fresh file exists."
    if event.area == "cron":
        match = re.search(r"\(([A-Za-z0-9_-]+)\)", event.message)
        job_id = match.group(1) if match else "unknown"
        return f"cron:{job_id}:{event.status}", "Classify by delivery/provider/script cause and escalate only on distinct failed runs."
    if event.area == "memory-capacity":
        return (
            f"memory-capacity:{event.status}",
            "Queue new learning safely; compact memory only through a separately reviewed, rollback-safe consolidation.",
        )
    return f"{event.area}:{event.status}", event.action or "Observe, classify, repair only through a verified low-risk rule."


def update_known_issues(events: list[Event], state: dict[str, Any], cycle_id: str) -> None:
    issues = state.setdefault("known_issues", {})
    active_keys: set[str] = set()
    for event in events:
        if event.level == "ok":
            continue
        key, remediation = issue_identity(event)
        active_keys.add(key)
        previous = issues.get(key, {})
        occurrences = int(previous.get("occurrences", 0))
        if previous.get("last_cycle") != cycle_id:
            occurrences += 1
        repaired = event.level == "action" and event.status not in {
            "would_restart",
            "would_regenerate",
            "would_disable_provider",
            "would_repair_qdrant",
        }
        issues[key] = {
            "key": key,
            "area": event.area,
            "last_status": event.status,
            "state": "repaired" if repaired else "active",
            "occurrences": occurrences,
            "first_seen": previous.get("first_seen") or now().isoformat(),
            "last_seen": now().isoformat(),
            "last_cycle": cycle_id,
            "message": event.message,
            "remediation": remediation,
        }
        if repaired:
            issues[key]["resolved_at"] = now().isoformat()
        else:
            issues[key].pop("resolved_at", None)

    for key, issue in list(issues.items()):
        if key in active_keys or issue.get("state") != "active":
            continue
        issue["state"] = "resolved"
        issue["resolved_at"] = now().isoformat()

    ordered = sorted(issues.values(), key=lambda item: item.get("last_seen", ""), reverse=True)
    ordered.sort(key=lambda item: item.get("state") == "resolved")
    ordered = ordered[:200]
    state["known_issues"] = {item["key"]: item for item in ordered}
    write_json(
        KNOWN_ISSUES_FILE,
        {
            "generated_at": now().isoformat(),
            "issues": ordered,
        },
    )


def write_report(events: list[Event], state: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{stamp()}.md"
    json_path = REPORT_DIR / f"{stamp()}.json"
    visible = [event for event in events if event.level != "ok"]

    lines = [
        f"# Hermes Self-Heal - {now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "## Summary",
        "",
        f"- Events: {len(events)}",
        f"- Non-ok events: {len(visible)}",
        f"- Notifications: {sum(1 for event in events if event.notify)}",
        "",
        "## Non-OK Events",
        "",
    ]
    if not visible:
        lines.append("- None.")
    for event in visible:
        suffix = f" action={event.action}" if event.action else ""
        notify = " notify=true" if event.notify else ""
        lines.append(f"- `{event.level}` `{event.area}` `{event.status}` {event.message}{suffix}{notify}")

    lines.extend(["", "## OK Checks", ""])
    ok_events = [event for event in events if event.level == "ok"]
    for event in ok_events[:80]:
        lines.append(f"- `{event.area}` `{event.status}` {event.message}")
    if len(ok_events) > 80:
        lines.append(f"- ... {len(ok_events) - 80} more ok checks omitted.")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    payload = {
        "generated_at": now().isoformat(),
        "events": [asdict(event) for event in events],
        "state": state,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, json_path


def notification_text(events: list[Event], md_path: Path) -> str:
    notify_events = [event for event in events if event.notify]
    if not notify_events:
        return ""
    lines = ["Hermes self-heal 需要关注："]
    for event in notify_events[:8]:
        lines.append(f"- {event.area}/{event.status}: {event.message}")
    if len(notify_events) > 8:
        lines.append(f"- 还有 {len(notify_events) - 8} 项，见本地报告。")
    lines.append("详细报告已保存在本机 Hermes reports 目录。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Hermes self-heal checks and low-risk repairs.")
    parser.add_argument("--dry-run", action="store_true", help="Do not apply repairs.")
    parser.add_argument("--print-clean", action="store_true", help="Print report path even when no notification is needed.")
    args = parser.parse_args()

    events: list[Event] = []
    state = load_state()
    cycle_id = stamp()

    check_hindsight(events, dry_run=args.dry_run)
    memory_infra_ok = check_qdrant_memory_infra(events, dry_run=args.dry_run)
    check_telegram_gateway(events)
    check_memory_config(events, dry_run=args.dry_run, memory_infra_ok=memory_infra_ok)
    enforce_controlled_write_gates(events, dry_run=args.dry_run)
    check_builtin_memory_capacity(events)
    check_astrology_semantics(events)
    check_capacity(events)
    check_reports(events, dry_run=args.dry_run)
    check_memory_governance_report(events, state)
    prune_generated_reports(events, dry_run=args.dry_run)

    jobs = load_jobs()
    check_script_health(events, jobs)
    check_cron_jobs(events, jobs, state)
    check_cron_prompt_memory_writes(events, jobs)
    update_known_issues(events, state, cycle_id)
    save_state(state)
    md_path, json_path = write_report(events, state)

    note = notification_text(events, md_path)
    if note:
        print(note)
    elif args.print_clean:
        visible = [event for event in events if event.level != "ok"]
        status = "clean" if not visible else f"{len(visible)} non-notifying observation(s)"
        print(f"Hermes self-heal {status}. Report: {md_path}; JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
