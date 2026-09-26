#!/usr/bin/env python3
"""Isolated fixture tests for hermes_official_release_trigger.evaluate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path.home() / ".hermes" / "scripts" / "hermes_official_release_trigger.py"
spec = importlib.util.spec_from_file_location("release_trigger", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

CASES = [
    (
        "CASE_A",
        dict(
            tags=["v2026.9.21", "v2026.9.24", "v0.22.0"],
            notes_by_tag={"v0.22.0": "Hermes Agent v0.22.0 stable minor"},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        True,
        "A:stable-minor",
    ),
    (
        "CASE_B",
        dict(
            tags=["v2026.9.21", "v2026.9.24", "v0.21.6"],
            notes_by_tag={"v0.21.6": "Desktop UI polish and plugin catalog additions"},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        False,
        "",
    ),
    (
        "CASE_C",
        dict(
            tags=["v2026.9.21", "v2026.9.24", "v0.21.6"],
            notes_by_tag={"v0.21.6": "fix: prevent data loss / data corruption in state.db crash consistency"},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        True,
        "B:critical-fix",
    ),
    (
        "CASE_D",
        dict(
            tags=["v2026.9.24", "v0.22.0-rc.1"],
            notes_by_tag={"v0.22.0-rc.1": "release candidate"},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        False,
        "",
    ),
    (
        "CASE_E",
        dict(
            # main-branch commits are not tags; only calver/semver tags counted.
            tags=["v2026.9.24", "v0.21.4+canary.20260926T065603Z", "rc.14-v0.21.5"],
            notes_by_tag={},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        False,
        "",
    ),
    (
        "CASE_F1_FIRST",
        dict(
            tags=["v2026.9.24", "v0.22.0"],
            notes_by_tag={"v0.22.0": "Hermes Agent v0.22.0"},
            last_seen_stable="v2026.9.24",
            last_notified_release="",
            last_trigger_reason="",
        ),
        True,
        "A:stable-minor",
    ),
    (
        "CASE_F2_SAME_AGAIN",
        dict(
            tags=["v2026.9.24", "v0.22.0"],
            notes_by_tag={"v0.22.0": "Hermes Agent v0.22.0"},
            last_seen_stable="v0.22.0",
            last_notified_release="v0.22.0",
            last_trigger_reason="A:stable-minor",
        ),
        False,
        "",
    ),
]


def main() -> int:
    failures = []
    for name, kwargs, expect_triggered, expect_reason in CASES:
        result = mod.evaluate(**kwargs)
        got_triggered = bool(result.get("notification_sent"))
        got_reason = result.get("reason") or ""
        ok = got_triggered == expect_triggered and (
            (not expect_triggered) or got_reason == expect_reason
        )
        status = "PASS" if ok else "FAIL"
        print(
            f"{status} {name}: triggered={got_triggered} reason={got_reason!r} "
            f"silent={result.get('silent_reason')!r} version={result.get('version')!r}"
        )
        if not ok:
            failures.append((name, result, expect_triggered, expect_reason))

    # Current live dry-run expectation
    print("--- current live dry-run ---")
    try:
        live = mod.evaluate(
            mod.fetch_tags(),
            {},
            last_seen_stable="",
            last_notified_release="",
            last_trigger_reason="",
        )
        print(
            f"LIVE triggered={live.get('notification_sent')} "
            f"version={live.get('version')} silent={live.get('silent_reason')!r}"
        )
        if live.get("notification_sent") is not False:
            print("FAIL LIVE_EXPECT_SILENT")
            failures.append(("LIVE", live, False, ""))
        else:
            print("PASS LIVE_EXPECT_SILENT")
    except Exception as exc:  # network optional in offline fixture
        print(f"SKIP LIVE (fetch failed): {type(exc).__name__}: {exc}")

    if failures:
        print(f"TOTAL_FAIL={len(failures)}")
        return 1
    print("ALL_FIXTURES_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
