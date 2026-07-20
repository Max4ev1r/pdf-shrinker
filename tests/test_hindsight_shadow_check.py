from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest


SOURCE = Path.home() / ".hermes" / "scripts" / "hindsight_shadow_check.py"


@pytest.fixture
def shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    name = f"_test_hindsight_shadow_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def write_benchmark(
    path: Path,
    generated_at: dt.datetime,
    *,
    passed: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "overall": "PASS" if passed else "WARN",
        "benchmark": {
            "version": 1,
            "default_suite": True,
            "real_recall": True,
            "all_passed": passed,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_health_only_markdown_does_not_count_as_real_recall(
    shadow,
    tmp_path: Path,
):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "20260717-040000.md").write_text(
        "Overall: PASS\n",
        encoding="utf-8",
    )
    now = dt.datetime(
        2026,
        7,
        18,
        10,
        tzinfo=dt.timezone(dt.timedelta(hours=8)),
    )

    streak = shadow.benchmark_pass_streak(
        report_dir,
        current_time=now,
        current_pass=True,
    )

    assert streak == 1


def test_seven_distinct_daily_passes_open_the_gate(
    shadow,
    tmp_path: Path,
):
    report_dir = tmp_path / "reports"
    timezone = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime(2026, 7, 18, 10, tzinfo=timezone)
    for offset in range(1, 7):
        generated = now - dt.timedelta(days=offset)
        write_benchmark(
            report_dir / f"{generated.strftime('%Y%m%d')}-040000.json",
            generated,
            passed=True,
        )

    gate = shadow.evaluate_promotion_gate(
        report_dir=report_dir,
        current_time=now,
        current_benchmark_pass=True,
        bank={"fresh": True},
        lifecycle={"verified": True},
        capabilities={
            "retain": True,
            "recall": True,
            "update": True,
            "forget": True,
        },
    )

    assert gate["current_pass_streak_days"] == 7
    assert gate["supplemental_retrieval_eligible"] is True
    assert gate["primary_provider_eligible"] is True


def test_latest_failure_for_a_day_breaks_the_streak(
    shadow,
    tmp_path: Path,
):
    report_dir = tmp_path / "reports"
    timezone = dt.timezone(dt.timedelta(hours=8))
    now = dt.datetime(2026, 7, 18, 10, tzinfo=timezone)
    yesterday = now - dt.timedelta(days=1)
    write_benchmark(
        report_dir / "20260717-040000.json",
        yesterday.replace(hour=4),
        passed=True,
    )
    write_benchmark(
        report_dir / "20260717-220000.json",
        yesterday.replace(hour=22),
        passed=False,
    )

    streak = shadow.benchmark_pass_streak(
        report_dir,
        current_time=now,
        current_pass=True,
    )

    assert streak == 1


def test_primary_gate_requires_update_forget_and_fresh_data(
    shadow,
    tmp_path: Path,
):
    now = dt.datetime(
        2026,
        7,
        18,
        10,
        tzinfo=dt.timezone(dt.timedelta(hours=8)),
    )

    gate = shadow.evaluate_promotion_gate(
        report_dir=tmp_path,
        current_time=now,
        current_benchmark_pass=False,
        bank={"fresh": False},
        lifecycle={"verified": False},
        capabilities={
            "retain": True,
            "recall": True,
            "update": False,
            "forget": False,
        },
    )

    assert gate["supplemental_retrieval_eligible"] is False
    assert gate["primary_provider_eligible"] is False
    assert any("fresh documents" in item for item in gate["primary_blockers"])
    assert any("explicit memory update" in item for item in gate["primary_blockers"])
    assert any("explicit memory forget" in item for item in gate["primary_blockers"])


def test_active_hindsight_config_wins_over_backup(shadow):
    config_dir = shadow.HERMES_HOME / "hindsight"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({
            "api_url": "http://127.0.0.1:8100",
            "bank_id": "active-bank",
        }),
        encoding="utf-8",
    )
    (config_dir / "config.json.bak").write_text(
        json.dumps({
            "api_url": "http://stale-backup:9999",
            "bank_id": "stale-bank",
        }),
        encoding="utf-8",
    )

    config = shadow.hindsight_config()

    assert config["api_url"] == "http://127.0.0.1:8100"
    assert config["bank_id"] == "active-bank"
