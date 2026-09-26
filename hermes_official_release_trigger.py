#!/usr/bin/env python3
"""Hermes Official Release Trigger — canonical cron job body.

Self-contained: rules live here so the job still works if Memory Vault is
unreadable. Silent (empty stdout) unless a trigger condition fires.

DEDUP state uses the job's durable cron notepad (NOT a new database):
  LAST_SEEN_STABLE / LAST_NOTIFIED_RELEASE / LAST_TRIGGER_REASON

Trigger notify only when:
  A. official stable v0.22.0 or higher stable minor
  B. pre-v0.22 stable patch with critical production-impacting fix
  C. new stable that covers/lowers v2026.9.24 HIGH-risk conflicts

Never notify for RC/canary/nightly/prerelease/main/UI/catalog/docs/CI.
Never auto-upgrade / auto-merge / auto-cherry-pick / auto-retire / modify production.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "NousResearch/hermes-agent"
REPO_GIT = f"https://github.com/{REPO}.git"
PRODUCTION_OFFICIAL_BASE = "v2026.9.21"
LAST_AUDITED_STABLE = "v2026.9.24"
LAST_AUDIT_DECISION = "UPGRADE_LATER"
MEMORY_RECORD_ID = "mem_cd6d551486c1"

# v2026.9.24 upgrade-audit HIGH-risk conflict files (condition C).
HIGH_RISK_CONFLICT_MARKERS = (
    "turn_finalizer.py",
    "delivery_ledger.py",
    "run_inbound.py",
    "memory_tool.py",
)

CRITICAL_FIX_KEYWORDS = (
    "security",
    "vulnerability",
    "cve-",
    "data loss",
    "data corruption",
    "corrupt",
    "crash consistency",
    "crash-consistency",
    "gateway",
    "profile",
    "multiplex",
    "cron",
    "kanban",
    "memory",
    "state.db",
    "plugin",
    "delivery",
    "trust boundary",
    "authorization",
    "authz",
    "update",
    "restart",
    "launchd",
)

# Strong production-impacting signals. When suppress/UI keywords are also
# present, only these count (so "plugin catalog" / "Desktop UI" never trigger).
STRONG_CRITICAL_FIX_KEYWORDS = (
    "security",
    "vulnerability",
    "cve-",
    "data loss",
    "data corruption",
    "data-corruption",
    "crash consistency",
    "crash-consistency",
    "trust boundary",
    "trust-boundary",
    "authorization",
    "authz",
    "memory corruption",
    "state.db corruption",
)

SUPPRESS_KEYWORDS = (
    "desktop",
    "/ui",
    " ui ",
    "ui-only",
    "ui only",
    "plugin catalog",
    "model catalog",
    "community plugin",
    "readme",
    "docs",
    "documentation",
    "ci",
    "build",
    "website",
    "chore",
    "style",
    "typo",
)

_UNSTABLE_RE = re.compile(
    r"(rc|canary|nightly|alpha|beta|pre|preview|dev|snapshot)",
    re.IGNORECASE,
)
_STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_CALVER_RE = re.compile(r"^v(\d{4})\.(\d{1,2})\.(\d+)$")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = _STABLE_TAG_RE.match(tag.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_stable_release_tag(tag: str) -> bool:
    tag = tag.strip()
    if not tag.startswith("v"):
        return False
    if _UNSTABLE_RE.search(tag):
        return False
    return parse_version(tag) is not None


# Dual-versioning bridge: calendar tags map onto the v0.21.x product line.
# v2026.9.21 == v0.21.4, v2026.9.24 == v0.21.5 (official release notes).
_CALVER_TO_SEMVER = {
    "v2026.9.7": (0, 21, 2),
    "v2026.9.11": (0, 21, 2),
    "v2026.9.14": (0, 21, 3),
    "v2026.9.21": (0, 21, 4),
    "v2026.9.24": (0, 21, 5),
}


def release_rank(tag: str) -> tuple[int, int, int] | None:
    """Sortable product version for a stable tag (calver and semver)."""
    tag = tag.strip()
    if tag in _CALVER_TO_SEMVER:
        return _CALVER_TO_SEMVER[tag]
    m = _CALVER_RE.match(tag)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Map calendar releases onto the v0.20 / v0.21 product line so
        # v2026.8.31 < v2026.9.24(=v0.21.5) < v0.21.6 < v0.22.0.
        if year < 2026 or (year == 2026 and month < 9):
            return (0, 20, day)
        if year == 2026 and month == 9:
            if day <= 24:
                return (0, 21, max(1, day - 19))
            return (0, 21, 5 + (day - 24))
        return (0, 21, 80 + month)
    return parse_version(tag)


def is_higher_stable_minor(tag: str, floor: str = "v0.22.0") -> bool:
    """True when tag is a stable at or above floor (v0.22.0+ stable minor)."""
    cur = release_rank(tag)
    base = release_rank(floor) or parse_version(floor)
    if not cur or not base:
        return False
    return cur >= base


def is_pre_v022_stable(tag: str) -> bool:
    cur = release_rank(tag)
    base = release_rank("v0.22.0") or (0, 22, 0)
    if not cur:
        return False
    return cur < base


def is_newer_than_audited(tag: str, audited: str = LAST_AUDITED_STABLE) -> bool:
    cur = release_rank(tag)
    old = release_rank(audited)
    if not cur or not old:
        return False
    return cur > old


def fetch_tags() -> list[str]:
    """Read-only tag list. Isolated from production working tree."""
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", REPO_GIT],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {proc.stderr.strip()[:300]}")
    tags: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/") :])
    return tags


def fetch_release_notes(tag: str) -> str:
    """Best-effort official release notes body. Empty string on failure."""
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-official-release-trigger",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return ""
    body = data.get("body") or ""
    name = data.get("name") or ""
    return f"{name}\n{body}"


def notepad_path() -> Path:
    return _hermes_home() / "cron" / "notepad.db"


def notepad_get(job_id: str, key: str) -> str:
    import sqlite3

    path = notepad_path()
    if not path.exists():
        return ""
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT value FROM cron_notepad WHERE job_id=? AND key=?",
            (str(job_id), str(key)),
        ).fetchone()
        return (row[0] if row else "") or ""
    finally:
        conn.close()


def notepad_set(job_id: str, key: str, value: str) -> None:
    import sqlite3

    path = notepad_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cron_notepad (
                job_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (job_id, key)
            )
            """,
        )
        conn.execute(
            """
            INSERT INTO cron_notepad(job_id, key, value, updated_at)
            VALUES(?,?,?,datetime('now'))
            ON CONFLICT(job_id, key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (str(job_id), str(key), str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _text_has_critical_fix(text: str) -> bool:
    low = text.lower()
    suppressed = any(k in low for k in SUPPRESS_KEYWORDS)
    if suppressed:
        return any(k in low for k in STRONG_CRITICAL_FIX_KEYWORDS)
    return any(k in low for k in CRITICAL_FIX_KEYWORDS)


def _text_covers_high_risk(text: str) -> list[str]:
    low = text.lower()
    hits = []
    for marker in HIGH_RISK_CONFLICT_MARKERS:
        stem = marker.replace(".py", "").replace("_", " ")
        if marker.lower() in low or stem in low:
            hits.append(marker)
    # Generic phrasing that clearly claims the audit conflict set.
    if "high-risk conflict" in low or "high risk conflict" in low:
        hits.extend(HIGH_RISK_CONFLICT_MARKERS)
    # de-dup preserve order
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def evaluate(
    tags: list[str],
    notes_by_tag: dict[str, str],
    *,
    last_seen_stable: str,
    last_notified_release: str,
    last_trigger_reason: str,
) -> dict:
    """Pure decision function (fixture-testable)."""
    stable_tags = [t for t in tags if is_stable_release_tag(t)]
    # Sort by product version ascending (calver and semver aware).
    stable_tags.sort(key=lambda t: release_rank(t) or (0, 0, 0))

    newest_stable = stable_tags[-1] if stable_tags else ""
    result = {
        "triggered": False,
        "notification_sent": False,
        "reason": "",
        "version": newest_stable,
        "deltas": [],
        "high_risk_conflict_change": "尚不能判断",
        "action": "暂不动作",
        "last_seen_stable": last_seen_stable or newest_stable,
        "last_notified_release": last_notified_release,
        "last_trigger_reason": last_trigger_reason,
        "silent_reason": "",
    }

    if not newest_stable:
        result["silent_reason"] = "no_stable_tag"
        return result

    # Track newest seen even when not notifying.
    result["last_seen_stable"] = newest_stable

    already_notified_same = (
        last_notified_release == newest_stable and bool(last_trigger_reason)
    )

    trigger = None
    reason = ""
    deltas: list[str] = []

    # A. v0.22.0+ stable minor
    if is_higher_stable_minor(newest_stable, "v0.22.0"):
        if last_notified_release != newest_stable or last_trigger_reason != "A:stable-minor":
            trigger = "A"
            reason = "A:stable-minor"
            deltas.append(f"official stable {newest_stable} (>= v0.22.0)")

    # B / C: stable newer than last audited (v2026.9.24 == v0.21.5), dual-version aware.
    if trigger is None and is_newer_than_audited(newest_stable, LAST_AUDITED_STABLE):
        notes = notes_by_tag.get(newest_stable, "")
        covered = _text_covers_high_risk(notes)
        if covered and (
            last_notified_release != newest_stable
            or last_trigger_reason != "C:high-risk-covered"
        ):
            trigger = "C"
            reason = "C:high-risk-covered"
            deltas.append(
                f"{newest_stable} notes cover HIGH-risk conflict files: {', '.join(covered)}"
            )
            result["high_risk_conflict_change"] = "减少"
        elif is_pre_v022_stable(newest_stable) and _text_has_critical_fix(notes):
            if (
                last_notified_release != newest_stable
                or last_trigger_reason != "B:critical-fix"
            ):
                trigger = "B"
                reason = "B:critical-fix"
                deltas.append(
                    f"{newest_stable} stable patch with critical production-impacting fix"
                )

    if trigger is None:
        result["silent_reason"] = (
            "already_notified"
            if already_notified_same
            else "no_trigger_condition"
        )
        return result

    # DEDUP: same release + same reason only once.
    if (
        last_notified_release == newest_stable
        and last_trigger_reason == reason
    ):
        result["silent_reason"] = "duplicate_suppressed"
        result["last_seen_stable"] = newest_stable
        return result

    result["triggered"] = True
    result["notification_sent"] = True
    result["reason"] = reason
    result["version"] = newest_stable
    result["deltas"] = deltas[:4]
    result["action"] = "重新开启 upgrade impact audit"
    result["last_notified_release"] = newest_stable
    result["last_trigger_reason"] = reason
    return result


def format_notification(result: dict) -> str:
    deltas = result.get("deltas") or []
    if not deltas:
        deltas = ["stable release detected"]
    change_block = "\n".join(f"- {d}" for d in deltas[:4])
    return (
        f"Hermes {result['version']}｜建议重新开启升级审计\n"
        f"\n"
        f"变化：\n"
        f"{change_block}\n"
        f"\n"
        f"对当前 Hermes：\n"
        f"- production official base = {PRODUCTION_OFFICIAL_BASE}\n"
        f"- last audited = {LAST_AUDITED_STABLE} ({LAST_AUDIT_DECISION})\n"
        f"- 涉及 capability / local patch：gateway/profile、cron/kanban、"
        f"memory/state.db、plugin/delivery API、trust boundary、update/restart/launchd"
        f"（以本次 release notes 实际条目为准）\n"
        f"\n"
        f"相对 v2026.9.24：\n"
        f"HIGH-risk conflict：{result.get('high_risk_conflict_change', '尚不能判断')}\n"
        f"\n"
        f"动作：\n"
        f"{result['action']}\n"
        f"\n"
        f"（禁止自动升级 / 自动 merge / 自动 cherry-pick / 自动 retire local patch / 修改 production）\n"
    )


def _job_id_from_env() -> str:
    for key in ("HERMES_CRON_JOB_ID", "HERMES_JOB_ID", "CRON_JOB_ID"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    # Stable fallback id used at create time; create writes real id here after.
    return os.environ.get("HERMES_RELEASE_TRIGGER_JOB_ID", "hermes-official-release-trigger")


def run(
    *,
    tags: list[str] | None = None,
    notes_by_tag: dict[str, str] | None = None,
    job_id: str | None = None,
    dry_run: bool = False,
) -> int:
    jid = job_id or _job_id_from_env()
    last_seen = notepad_get(jid, "LAST_SEEN_STABLE") if not dry_run else ""
    last_notified = notepad_get(jid, "LAST_NOTIFIED_RELEASE") if not dry_run else ""
    last_reason = notepad_get(jid, "LAST_TRIGGER_REASON") if not dry_run else ""

    if tags is None:
        tags = fetch_tags()
    if notes_by_tag is None:
        notes_by_tag = {}

    # Lazy-fetch notes only for the newest stable when needed.
    stable_tags = [t for t in tags if is_stable_release_tag(t)]
    if stable_tags:
        stable_tags.sort(key=lambda t: release_rank(t) or (0, 0, 0))
        newest = stable_tags[-1]
        if newest not in notes_by_tag and newest != LAST_AUDITED_STABLE:
            notes_by_tag[newest] = fetch_release_notes(newest)

    result = evaluate(
        tags,
        notes_by_tag,
        last_seen_stable=last_seen,
        last_notified_release=last_notified,
        last_trigger_reason=last_reason,
    )

    if not dry_run:
        if result.get("last_seen_stable"):
            notepad_set(jid, "LAST_SEEN_STABLE", result["last_seen_stable"])
        if result.get("notification_sent"):
            notepad_set(jid, "LAST_NOTIFIED_RELEASE", result["last_notified_release"])
            notepad_set(jid, "LAST_TRIGGER_REASON", result["last_trigger_reason"])

    if result.get("notification_sent"):
        sys.stdout.write(format_notification(result))
        return 0

    # Silent: empty stdout → no delivery.
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    json_out = "--json" in argv
    if json_out:
        # Machine-readable evaluation for fixtures (never delivered).
        tags = None
        notes: dict[str, str] = {}
        # Optional fixture injection via env for tests only.
        fixture_tags = os.environ.get("HERMES_RELEASE_TRIGGER_FIXTURE_TAGS")
        fixture_notes = os.environ.get("HERMES_RELEASE_TRIGGER_FIXTURE_NOTES")
        if fixture_tags:
            tags = json.loads(fixture_tags)
        if fixture_notes:
            notes = json.loads(fixture_notes)
        jid = _job_id_from_env()
        result = evaluate(
            tags if tags is not None else (fetch_tags() if not fixture_tags else tags or []),
            notes,
            last_seen_stable=os.environ.get("HERMES_RELEASE_TRIGGER_FIXTURE_LAST_SEEN", ""),
            last_notified_release=os.environ.get("HERMES_RELEASE_TRIGGER_FIXTURE_LAST_NOTIFIED", ""),
            last_trigger_reason=os.environ.get("HERMES_RELEASE_TRIGGER_FIXTURE_LAST_REASON", ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return run(dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
