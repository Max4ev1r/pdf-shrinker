#!/usr/bin/env python3
"""Observe Telegram gateway health without mutating Telegram state.

This guard probes only read-only surfaces:
- Hermes-managed TELEGRAM_BOT_TOKEN presence
- the proxy URL Hermes would resolve for Telegram
- Telegram Bot API getMe
- recent gateway log Telegram reconnect/error activity

It never sends a chat message, changes webhook state, or prints secrets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR", str(HERMES_HOME / "hermes-agent"))
).expanduser()
CONFIG_FILE = HERMES_HOME / "config.yaml"
GATEWAY_LOG = HERMES_HOME / "logs" / "gateway.log"
ENV_FILE = HERMES_HOME / ".env"
TELEGRAM_API_HOST = "api.telegram.org"
LOG_LOOKBACK_HOURS = 2.0
FLAP_ERROR_THRESHOLD = 3


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def ensure_agent_imports() -> None:
    path = str(HERMES_AGENT_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def read_dotenv_value(key: str) -> str:
    if not ENV_FILE.exists():
        return ""
    key_re = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*)\s*$")
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = key_re.match(line)
            if not match:
                continue
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value.strip()
    except Exception:
        return ""
    return ""


def get_env_value(key: str) -> str:
    dotenv_value = read_dotenv_value(key)
    if dotenv_value:
        return dotenv_value
    ensure_agent_imports()
    try:
        from hermes_cli.config import get_env_value as _get_env_value

        return str(_get_env_value(key) or "").strip()
    except Exception:
        return str(os.environ.get(key, "") or "").strip()


def resolve_telegram_proxy() -> str:
    ensure_agent_imports()
    try:
        from gateway.platforms.base import resolve_proxy_url

        return str(resolve_proxy_url("TELEGRAM_PROXY", target_hosts=[TELEGRAM_API_HOST]) or "")
    except Exception:
        for key in ("TELEGRAM_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            value = str(os.environ.get(key, "") or "").strip()
            if value:
                return value
    return ""


def redact(text: Any, token: str = "") -> str:
    value = str(text or "")
    if token:
        value = value.replace(token, "[REDACTED_TOKEN]")
        value = value.replace(f"bot{token}", "bot[REDACTED_TOKEN]")
    return re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot[REDACTED_TOKEN]", value)


def token_fingerprint(token: str) -> str:
    if not token:
        return ""
    if ":" in token:
        prefix = token.split(":", 1)[0]
    else:
        prefix = token[:4]
    return f"{prefix}:[REDACTED]"


def telegram_enabled_without_token() -> bool:
    env_enabled = get_env_value("TELEGRAM_ENABLED").lower()
    if env_enabled in {"1", "true", "yes", "on"}:
        return True
    if not CONFIG_FILE.exists():
        return False
    try:
        import yaml

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        telegram = data.get("telegram", {}) if isinstance(data, dict) else {}
        gateway = data.get("gateway", {}) if isinstance(data, dict) else {}
        platforms = gateway.get("platforms", []) if isinstance(gateway, dict) else []
        if isinstance(telegram, dict) and str(telegram.get("enabled", "")).lower() in {"1", "true", "yes", "on"}:
            return True
        if isinstance(platforms, list) and "telegram" in [str(item).lower() for item in platforms]:
            return True
    except Exception:
        return False
    return False


def tcp_probe(proxy_url: str) -> dict[str, Any]:
    if not proxy_url:
        return {"configured": False, "reachable": False, "host": "", "port": 0, "error": ""}
    parsed = urllib.parse.urlparse(proxy_url)
    host = parsed.hostname or ""
    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    if not host:
        return {"configured": True, "reachable": False, "host": "", "port": port, "error": "proxy host missing"}
    try:
        with socket.create_connection((host, int(port)), timeout=2.0):
            return {"configured": True, "reachable": True, "host": host, "port": int(port), "error": ""}
    except OSError as exc:
        return {
            "configured": True,
            "reachable": False,
            "host": host,
            "port": int(port),
            "error": f"{type(exc).__name__}: {exc}",
        }


def telegram_get_me(token: str, proxy_url: str) -> dict[str, Any]:
    if not token:
        return {"ok": False, "status": 0, "error": "TELEGRAM_BOT_TOKEN missing"}
    url = f"https://{TELEGRAM_API_HOST}/bot{token}/getMe"
    handlers: list[Any] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "HermesTelegramGuard/1"})
        with opener.open(req, timeout=8) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        return {
            "ok": bool(payload.get("ok")),
            "status": int(getattr(resp, "status", 0) or 0),
            "bot": {
                "id": result.get("id", ""),
                "username": result.get("username", ""),
                "first_name": result.get("first_name", ""),
            } if isinstance(result, dict) else {},
            "error": "",
        }
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {"ok": False, "status": int(exc.code), "error": redact(body or exc.reason, token)}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": redact(f"{type(exc).__name__}: {exc}", token)}


def parse_log_time(line: str) -> dt.datetime | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", line)
    if not match:
        return None
    raw = f"{match.group(1)}.{match.group(2)}"
    try:
        parsed = dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f")
        return parsed.replace(tzinfo=now().tzinfo)
    except ValueError:
        return None


def recent_gateway_log() -> dict[str, Any]:
    if not GATEWAY_LOG.exists():
        return {
            "exists": False,
            "lookback_hours": LOG_LOOKBACK_HOURS,
            "error_count": 0,
            "success_count": 0,
            "last_error_at": "",
            "last_success_at": "",
            "last_error": "",
            "flapping": False,
        }
    cutoff = now() - dt.timedelta(hours=LOG_LOOKBACK_HOURS)
    error_patterns = (
        "Telegram network error",
        "Connect attempt",
        "Reconnect telegram error",
        "telegram connect timed out",
        "This HTTPXRequest is not initialized",
    )
    success_patterns = (
        "Connected to Telegram",
        "telegram reconnected successfully",
        "Telegram polling resumed",
    )
    error_count = 0
    success_count = 0
    last_error_at = ""
    last_success_at = ""
    last_error = ""
    try:
        lines = GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]
    except Exception:
        lines = []
    for line in lines:
        timestamp = parse_log_time(line)
        if timestamp is not None and timestamp < cutoff:
            continue
        if "[Telegram]" not in line and "telegram" not in line:
            continue
        if any(pattern in line for pattern in error_patterns):
            error_count += 1
            last_error_at = timestamp.isoformat(timespec="seconds") if timestamp else ""
            last_error = line[:500]
        if any(pattern in line for pattern in success_patterns):
            success_count += 1
            last_success_at = timestamp.isoformat(timespec="seconds") if timestamp else ""
    return {
        "exists": True,
        "lookback_hours": LOG_LOOKBACK_HOURS,
        "error_count": error_count,
        "success_count": success_count,
        "last_error_at": last_error_at,
        "last_success_at": last_success_at,
        "last_error": redact(last_error),
        "flapping": error_count >= FLAP_ERROR_THRESHOLD and success_count > 0,
    }


def check() -> dict[str, Any]:
    token = get_env_value("TELEGRAM_BOT_TOKEN")
    proxy_url = resolve_telegram_proxy()
    proxy = tcp_probe(proxy_url)
    get_me = telegram_get_me(token, proxy_url)
    log = recent_gateway_log()
    enabled_without_token = not token and telegram_enabled_without_token()

    status = "healthy"
    level = "ok"
    if enabled_without_token:
        status = "token_missing"
        level = "critical"
    elif token and proxy.get("configured") and not proxy.get("reachable"):
        status = "proxy_down"
        level = "critical"
    elif token and not get_me.get("ok"):
        status = "api_unreachable"
        level = "warning"
    elif token and log.get("flapping"):
        status = "flapping"
        level = "warning"
    elif not token:
        status = "not_configured"

    healthy = status in {"healthy", "not_configured", "flapping"}
    return {
        "generated_at": now_iso(),
        "healthy": healthy,
        "level": level,
        "status": status,
        "token_present": bool(token),
        "token_fingerprint": token_fingerprint(token),
        "proxy_url": redact(proxy_url, token),
        "proxy": proxy,
        "get_me": get_me,
        "gateway_log": log,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Hermes Telegram gateway health.")
    parser.add_argument("--check", action="store_true", help="Check Telegram health.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()
    if not args.check:
        parser.error("--check is required")
    payload = check()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Hermes Telegram guard: {payload['status']}")
    return 0 if payload["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
