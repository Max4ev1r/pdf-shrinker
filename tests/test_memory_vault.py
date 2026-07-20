from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


VAULT_SOURCE = Path.home() / ".hermes" / "scripts" / "memory_vault.py"
PLUGIN_SOURCE = Path.home() / ".hermes" / "plugins" / "vault" / "__init__.py"


class FakeEmbedder:
    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        return [
            1.0 + lowered.count("空调") + lowered.count("冷气"),
            1.0 + lowered.count("护肤") + lowered.count("刺痛"),
            1.0 + lowered.count("music") + lowered.count("音质"),
            1.0 + len(lowered) % 7,
        ]

    def embed(self, texts):
        return iter(self._vector(text) for text in texts)

    def query_embed(self, texts):
        return iter(self._vector(text) for text in texts)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_AGENT_DIR", str(tmp_path / "hermes-agent"))
    name = f"_test_memory_vault_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, VAULT_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.get_local_embedder = lambda: FakeEmbedder()
    module.ensure_layout()
    yield module
    sys.modules.pop(name, None)


def test_rejected_candidate_can_be_observed_again(vault):
    first, created = vault.propose_record(
        "用户长期偏好红色界面",
        topic="other",
        source="test",
        evidence_session="session-a",
    )
    assert created
    vault.reject_record(first["id"], reason="not confirmed")

    second, created = vault.propose_record(
        "用户长期偏好红色界面",
        topic="other",
        source="test",
        evidence_session="session-b",
    )

    assert created
    assert second["id"] != first["id"]
    statuses = {
        record["id"]: record["status"]
        for record in vault.read_jsonl(vault.RECORDS_PATH)
    }
    assert statuses == {first["id"]: "rejected", second["id"]: "pending"}


def test_rejected_update_can_be_observed_again(vault):
    active, _ = vault.add_record("用户以前偏好红色", topic="other")
    first, _ = vault.propose_update(
        active["id"],
        "用户现在偏好蓝色",
        source="test",
        evidence_session="session-a",
    )
    vault.reject_record(first["id"], reason="not confirmed")

    second, created = vault.propose_update(
        active["id"],
        "用户现在偏好蓝色",
        source="test",
        evidence_session="session-b",
    )

    assert created
    assert second["id"] != first["id"]


def test_superseded_version_cannot_be_updated_or_reactivated(vault):
    old, _ = vault.add_record("用户以前偏好红色", topic="other")
    new, _ = vault.propose_update(
        old["id"],
        "用户现在偏好蓝色",
        source="test",
        evidence_session="session-a",
    )
    vault.merge_record(new["id"], old["id"])

    with pytest.raises(SystemExit, match="not active"):
        vault.update_record(old["id"], "用户又改成绿色")
    with pytest.raises(SystemExit, match="Unsafe status transition"):
        vault.set_status(old["id"], "active")

    active = [
        record for record in vault.read_jsonl(vault.RECORDS_PATH)
        if record["status"] == "active"
    ]
    assert [record["id"] for record in active] == [new["id"]]


def test_merge_candidate_cannot_be_promoted_without_superseding_match(vault):
    old, _ = vault.add_record("用户以前偏好红色", topic="other")
    candidate, _ = vault.propose_update(
        old["id"],
        "用户现在偏好蓝色",
        source="test",
        evidence_session="session-a",
    )

    with pytest.raises(SystemExit, match="use merge"):
        vault.promote_record(candidate["id"])


def test_secret_is_rejected_before_records_events_or_exports(vault):
    before = vault.database_health()
    secret = "api_key = sk-" + "a" * 32

    with pytest.raises(vault.SecretMemoryRejected):
        vault.propose_record(
            secret,
            topic="other",
            source="test",
            evidence_session="session-a",
        )

    after = vault.database_health()
    assert after["record_count"] == before["record_count"]
    assert after["event_count"] == before["event_count"]
    assert secret not in vault.RECORDS_PATH.read_text(encoding="utf-8")
    assert secret not in vault.HISTORY_PATH.read_text(encoding="utf-8")


def test_same_long_lived_session_can_supply_evidence_on_later_day(
    vault,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = {"value": "2026-07-18T10:00:00+08:00"}
    monkeypatch.setattr(vault, "now_iso", lambda: clock["value"])
    record, _ = vault.propose_record(
        "用户偏好蓝色界面",
        topic="other",
        source="test",
        evidence_session="long-session",
    )
    vault.propose_record(
        "用户偏好蓝色界面",
        topic="other",
        source="test",
        evidence_session="long-session",
    )
    assert vault.evidence_count(record["id"]) == 1

    clock["value"] = "2026-07-19T10:00:00+08:00"
    vault.propose_record(
        "用户偏好蓝色界面",
        topic="other",
        source="test",
        evidence_session="long-session",
    )
    assert vault.evidence_count(record["id"]) == 2


def test_limited_mem0_sync_never_deletes_unprocessed_active_records(
    vault,
    monkeypatch: pytest.MonkeyPatch,
):
    first, _ = vault.add_record("第一条长期事实", topic="other")
    second, _ = vault.add_record("第二条长期事实", topic="other")
    existing = {
        first["id"]: [{"id": "index-1", "memory": vault.mem0_text(first)}],
        second["id"]: [{"id": "index-2", "memory": vault.mem0_text(second)}],
    }

    class Provider:
        def __init__(self):
            self.calls = []
            self._backend = object()

        def handle_tool_call(self, name, args):
            self.calls.append((name, args))
            return json.dumps({"result": "ok"})

        def shutdown(self):
            return None

    provider = Provider()
    monkeypatch.setattr(
        vault,
        "mem0_readiness",
        lambda: {"ready_for_shadow": True},
    )
    monkeypatch.setattr(vault, "load_mem0_provider", lambda: provider)
    monkeypatch.setattr(
        vault,
        "list_mem0_vault_items",
        lambda provider, top_k: (existing, []),
    )

    result = vault.sync_mem0(limit=1)

    assert result["partial"] is True
    assert result["active_records"] == 2
    assert not [
        call for call in provider.calls
        if call[0] == "mem0_delete" and call[1]["memory_id"] == "index-2"
    ]


def test_outbox_job_reconciles_even_without_pending_events(
    vault,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []
    monkeypatch.setattr(vault, "pending_index_events", lambda: [])
    monkeypatch.setattr(
        vault,
        "sync_mem0",
        lambda: calls.append("sync") or {"errors": []},
    )

    result = vault.sync_index_outbox()

    assert calls == ["sync"]
    assert result["pending_events"] == 0
    assert result["reconciled"] is True


def test_active_vault_provider_reports_production_index_ready(vault):
    (vault.HERMES_HOME / "config.yaml").write_text(
        "memory:\n  provider: vault\n",
        encoding="utf-8",
    )
    (vault.HERMES_HOME / "mem0.json").write_text(
        json.dumps({"mode": "oss", "oss": {}}),
        encoding="utf-8",
    )

    readiness = vault.mem0_readiness()

    assert readiness["active_memory_provider"] == "vault"
    assert readiness["ready_for_shadow"] is True
    assert readiness["ready_for_production"] is True


def test_fts_aliases_find_home_assistant_from_natural_chinese(vault):
    expected, _ = vault.add_record(
        "Home Assistant 已接入，空调控制使用 ha_control.py 和 ha_set_temp.py。",
        topic="hermes_ops",
    )
    vault.add_record("用户喜欢蓝色界面", topic="other")

    results = vault.local_search("家里的冷气要通过哪个脚本调温？", top_k=3)

    assert results
    assert results[0]["id"] == expected["id"]


def test_local_index_rebuild_is_safe_across_processes(vault, tmp_path: Path):
    vault.add_record("Home Assistant 空调控制使用 ha_control.py", topic="hermes_ops")
    worker = tmp_path / "index_worker.py"
    worker.write_text(
        f"""
import importlib.util
import os
import sys
import uuid

class Fake:
    def embed(self, texts):
        return iter([1.0, 0.0, 0.0, 1.0] for _ in texts)

spec = importlib.util.spec_from_file_location("_worker_" + uuid.uuid4().hex, {str(VAULT_SOURCE)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.get_local_embedder = lambda: Fake()
records = module.read_jsonl(module.RECORDS_PATH)
for _ in range(8):
    module.rebuild_local_index(records)
""".lstrip(),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(vault.HERMES_HOME)
    env["HERMES_AGENT_DIR"] = str(vault.HERMES_AGENT_DIR)
    processes = [
        subprocess.Popen(
            [sys.executable, str(worker)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        if process.returncode:
            failures.append((process.returncode, stdout, stderr))

    assert failures == []
    assert not list(vault.VAULT_DIR.glob(".local-search.sqlite3.*.tmp"))
    with sqlite3.connect(vault.LOCAL_INDEX_PATH) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def load_vault_provider_module():
    name = f"_test_vault_provider_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def test_explicit_memory_parser_is_narrow():
    name, plugin = load_vault_provider_module()
    try:
        parse = plugin.VaultMemoryProvider._explicit_memory_content
        assert parse("请记住：我以后偏好蓝色界面") == "我以后偏好蓝色界面"
        assert parse("Remember that I prefer concise answers.") == "I prefer concise answers."
        assert parse("不要记住：这是临时测试") == ""
        assert parse("你还记得我喜欢什么颜色吗？") == ""
        assert parse("普通聊天里提到记忆，但没有要求写入") == ""
    finally:
        sys.modules.pop(name, None)


def test_explicit_low_risk_request_is_promoted_without_storing_raw_turn():
    name, plugin = load_vault_provider_module()

    class FakeVault:
        class SecretMemoryRejected(ValueError):
            pass

        def __init__(self):
            self.proposals = []
            self.promotions = []

        def propose_record(self, content, **kwargs):
            self.proposals.append((content, kwargs))
            return {
                "id": "candidate-1",
                "status": "pending",
                "risk": "low",
                "governance_action": "add",
                "matched_id": "",
            }, True

        def promote_record(self, record_id, **kwargs):
            self.promotions.append((record_id, kwargs))

    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = FakeVault()
        provider._session_id = "provider-session"
        provider.sync_turn(
            "记住：我以后偏好蓝色界面",
            "好的，我会记住。",
            session_id="turn-session",
        )

        assert provider._vault.proposals == [
            (
                "我以后偏好蓝色界面",
                {
                    "source": "turn_explicit_memory_request",
                    "evidence_session": "turn-session",
                    "metadata": {"source_type": "user_requested_memory"},
                },
            )
        ]
        assert provider._vault.promotions == [
            ("candidate-1", {"reason": "explicit user request to remember"})
        ]
    finally:
        sys.modules.pop(name, None)
