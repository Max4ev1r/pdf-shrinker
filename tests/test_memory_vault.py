from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest


VAULT_SOURCE = Path.home() / ".hermes" / "scripts" / "memory_vault.py"
PLUGIN_SOURCE = Path.home() / ".hermes" / "plugins" / "vault" / "__init__.py"
RETRIEVAL_EVAL_SOURCE = (
    Path.home() / ".hermes" / "scripts" / "memory_retrieval_eval.py"
)
CONTROLLED_LEARNING_SOURCE = (
    Path.home() / ".hermes" / "scripts" / "hermes_controlled_learning.py"
)
LEARNING_ACTIONS_SOURCE = (
    Path.home() / ".hermes" / "scripts" / "hermes_learning_actions.py"
)
SELF_HEAL_SOURCE = (
    Path.home() / ".hermes" / "scripts" / "hermes_self_heal.py"
)
MEMORY_GOVERNOR_SOURCE = (
    Path.home() / ".hermes" / "scripts" / "memory_governor.py"
)


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

    def embed(self, texts, **kwargs):
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
    module._real_get_local_embedder = module.get_local_embedder
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


def test_repeating_current_update_is_idempotent(vault):
    active, _ = vault.add_record("用户长期偏好蓝色", topic="other")

    unchanged, created = vault.propose_update(
        active["id"],
        "用户长期偏好蓝色",
        source="test",
        evidence_session="session-a",
    )

    assert not created
    assert unchanged["id"] == active["id"]
    records = vault.read_jsonl(vault.RECORDS_PATH)
    assert len(records) == 1
    assert records[0]["status"] == "active"


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


def test_review_rebases_sibling_update_onto_current_successor(vault):
    old, _ = vault.add_record("用户以前偏好红色", topic="other")
    first, _ = vault.propose_update(
        old["id"],
        "用户现在偏好蓝色",
        source="test",
        evidence_session="session-a",
    )
    second, _ = vault.propose_update(
        old["id"],
        "用户现在偏好绿色",
        source="test",
        evidence_session="session-b",
    )

    vault.review_record(first["id"], "approve")
    merged = vault.review_record(second["id"], "approve")

    records = {row["id"]: row for row in vault.read_jsonl(vault.RECORDS_PATH)}
    assert records[old["id"]]["status"] == "superseded"
    assert records[first["id"]]["status"] == "superseded"
    assert records[first["id"]]["superseded_by"] == second["id"]
    assert merged["status"] == "active"
    assert merged["matched_id"] == first["id"]
    assert merged["source"]["supersedes"] == first["id"]
    assert merged["source"]["requested_supersedes"] == old["id"]

    event = vault.read_jsonl(vault.HISTORY_PATH)[-1]
    assert event["requested_active_id"] == old["id"]
    assert event["resolved_active_id"] == first["id"]


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


def test_vault_writes_do_not_enqueue_retired_external_index(vault):
    record, created = vault.add_record("本地 Vault 写入不应进入外部索引队列", topic="other")

    assert created
    with sqlite3.connect(vault.VAULT_DB_PATH) as conn:
        queued = conn.execute(
            "SELECT COUNT(*) FROM index_outbox WHERE record_id = ?",
            (record["id"],),
        ).fetchone()[0]
    assert queued == 0


def test_active_vault_provider_does_not_require_retired_shadow(vault):
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
    assert readiness["vault_local_only"] is True
    assert readiness["ready_for_shadow"] is False
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


def test_fts_vehicle_aliases_recall_car_from_natural_chinese(
    vault,
    monkeypatch,
):
    def unavailable():
        raise ModuleNotFoundError("fastembed")

    monkeypatch.setattr(vault, "get_local_embedder", unavailable)
    expected, _ = vault.add_record(
        "用户目前驾驶特斯拉 Model Y。",
        topic="products",
    )

    results = vault.local_search("我平时开什么车？", top_k=3)

    assert results
    assert results[0]["id"] == expected["id"]


def test_local_search_filters_weak_candidates_before_ranking(
    vault,
    monkeypatch,
):
    manager = vault._local_index_manager()
    sqlite3.connect(vault.LOCAL_INDEX_PATH).close()
    monkeypatch.setattr(manager, "ensure", lambda records: False)
    monkeypatch.setattr(manager, "fts_search", lambda conn, query, top_k: [
        {"id": "dual", "title": "dual", "updated_at": "2026", "rank": -50.0},
        {"id": "weak", "title": "weak", "updated_at": "2026", "rank": -5.0},
    ])
    monkeypatch.setattr(manager, "vector_search", lambda conn, query, top_k: [
        {"id": "semantic", "title": "semantic", "updated_at": "2026", "semantic_score": 0.70},
        {"id": "relative-tail", "title": "tail", "updated_at": "2026", "semantic_score": 0.59},
        {"id": "dual", "title": "dual", "updated_at": "2026", "semantic_score": 0.50},
        {"id": "weak", "title": "weak", "updated_at": "2026", "semantic_score": 0.55},
    ])

    results = manager.search([], "query", top_k=10)

    assert [row["id"] for row in results] == ["dual", "semantic"]


def test_local_search_allows_zero_results(vault, monkeypatch):
    manager = vault._local_index_manager()
    sqlite3.connect(vault.LOCAL_INDEX_PATH).close()
    monkeypatch.setattr(manager, "ensure", lambda records: False)
    monkeypatch.setattr(manager, "fts_search", lambda conn, query, top_k: [
        {"id": "noise", "title": "noise", "updated_at": "2026", "rank": -4.0},
    ])
    monkeypatch.setattr(manager, "vector_search", lambda conn, query, top_k: [
        {"id": "noise", "title": "noise", "updated_at": "2026", "semantic_score": 0.45},
    ])

    assert manager.search([], "query", top_k=10) == []


def test_local_search_keeps_lexical_hits_when_embeddings_are_unavailable(
    vault,
    monkeypatch,
):
    manager = vault._local_index_manager()
    sqlite3.connect(vault.LOCAL_INDEX_PATH).close()
    monkeypatch.setattr(manager, "ensure", lambda records: False)
    monkeypatch.setattr(manager, "fts_search", lambda conn, query, top_k: [
        {
            "id": "exact",
            "title": "query preference",
            "search_tokens": "query preference",
            "updated_at": "2026",
            "rank": -4.0,
        },
    ])
    monkeypatch.setattr(manager, "vector_search", lambda conn, query, top_k: [])

    assert [row["id"] for row in manager.search([], "query", top_k=10)] == ["exact"]


def test_local_search_rejects_single_weak_lexical_overlap(
    vault,
    monkeypatch,
):
    manager = vault._local_index_manager()
    sqlite3.connect(vault.LOCAL_INDEX_PATH).close()
    monkeypatch.setattr(manager, "ensure", lambda records: False)
    monkeypatch.setattr(manager, "fts_search", lambda conn, query, top_k: [{
        "id": "location",
        "title": "用户常住无锡",
        "summary": "用户常住无锡",
        "body": "用户常住无锡",
        "search_tokens": "无锡",
        "updated_at": "2026",
        "rank": -4.0,
    }])
    monkeypatch.setattr(manager, "vector_search", lambda conn, query, top_k: [])

    assert manager.search([], "无锡今天会下雨吗？", top_k=10) == []


def test_local_search_keeps_specific_lexical_fact_without_embeddings(
    vault,
    monkeypatch,
):
    manager = vault._local_index_manager()
    sqlite3.connect(vault.LOCAL_INDEX_PATH).close()
    monkeypatch.setattr(manager, "ensure", lambda records: False)
    monkeypatch.setattr(manager, "fts_search", lambda conn, query, top_k: [{
        "id": "health",
        "title": "高血压病史",
        "summary": "用户有高血压病史",
        "body": "用户有高血压病史",
        "search_tokens": "高血压 高血 血压",
        "updated_at": "2026",
        "rank": -8.0,
    }])
    monkeypatch.setattr(manager, "vector_search", lambda conn, query, top_k: [])

    results = manager.search([], "我的高血压记录", top_k=10)

    assert [row["id"] for row in results] == ["health"]


def test_local_index_health_reports_real_retrieval_mode(vault, monkeypatch):
    real_find_spec = vault.importlib.util.find_spec
    monkeypatch.setattr(
        vault.importlib.util,
        "find_spec",
        lambda name: object() if name == "fastembed" else real_find_spec(name),
    )
    vault.add_record("用户长期驾驶特斯拉", topic="other")

    ready = vault.local_index_health()

    assert ready["index_ready"] is True
    assert ready["semantic_ready"] is True
    assert ready["mode"] == "local-hybrid"
    assert ready["vector_records"] == ready["active_records"] == 1

    def unavailable():
        raise ModuleNotFoundError("fastembed")

    monkeypatch.setattr(vault, "get_local_embedder", unavailable)
    vault.rebuild_local_index(vault.read_jsonl(vault.RECORDS_PATH))

    degraded = vault.local_index_health()

    assert degraded["index_ready"] is True
    assert degraded["semantic_ready"] is False
    assert degraded["mode"] == "lexical-only"
    assert degraded["embedding_status"] == "disabled:dependency-not-installed"
    assert degraded["vector_records"] == 0
    assert vault.local_search_mode() == "lexical-only"


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
    def embed(self, texts, **kwargs):
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


def test_local_embedder_uses_durable_hermes_cache(vault, monkeypatch):
    calls = []

    class Embedder:
        def __init__(self, *, model_name, cache_dir, threads):
            calls.append((model_name, cache_dir, threads))

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = Embedder
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    vault._LOCAL_EMBEDDER = None

    embedder = vault._real_get_local_embedder()

    assert isinstance(embedder, Embedder)
    assert calls == [(
        vault.LOCAL_EMBEDDING_MODEL,
        str(vault.HERMES_HOME / "cache" / "fastembed"),
        1,
    )]


def load_vault_provider_module():
    name = f"_test_vault_provider_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def load_retrieval_eval_module():
    name = f"_test_memory_retrieval_eval_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, RETRIEVAL_EVAL_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def load_controlled_learning_module():
    name = f"_test_controlled_learning_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name,
        CONTROLLED_LEARNING_SOURCE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def load_learning_actions_module():
    name = f"_test_learning_actions_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, LEARNING_ACTIONS_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def load_self_heal_module():
    name = f"_test_self_heal_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, SELF_HEAL_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def load_memory_governor_module():
    name = f"_test_memory_governor_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name,
        MEMORY_GOVERNOR_SOURCE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


def test_production_retrieval_accepts_local_hot_path():
    name, retrieval = load_retrieval_eval_module()

    class Provider:
        @staticmethod
        def handle_tool_call(tool_name, args):
            assert tool_name == "vault_search"
            assert args["top_k"] == 10
            return json.dumps({
                "results": [{"id": "mem_expected", "summary": "expected"}],
                "mode": "local-hybrid",
            })

    try:
        result = retrieval.evaluate_case(
            Provider(),
            {
                "name": "local-hot-path",
                "query": "query",
                "expected_any": ["mem_expected"],
            },
            accepted_modes={"hybrid", "local-hybrid"},
        )

        assert result["passed"]
        assert result["content_passed"]
        assert result["mode_passed"]
        assert result["error"] == ""
    finally:
        sys.modules.pop(name, None)


def test_retrieval_eval_requires_negative_queries_to_abstain():
    name, retrieval = load_retrieval_eval_module()

    class Provider:
        @staticmethod
        def handle_tool_call(tool_name, args):
            return json.dumps({
                "results": [{"id": "mem_noise", "summary": "noise"}],
                "mode": "local-hybrid",
            })

    try:
        result = retrieval.evaluate_case(
            Provider(),
            {
                "name": "negative",
                "query": "hello",
                "expected_any": [],
                "allowed_ids": [],
                "expect_empty": True,
            },
            accepted_modes={"local-hybrid"},
        )

        assert not result["passed"]
        assert not result["content_passed"]
        assert not result["precision_passed"]
    finally:
        sys.modules.pop(name, None)


def test_self_heal_resolves_gateway_interpreter_from_launchd(
    tmp_path,
    monkeypatch,
):
    name, self_heal = load_self_heal_module()
    try:
        interpreter = tmp_path / "gateway-python"
        interpreter.touch()
        plist = tmp_path / "ai.hermes.gateway.plist"
        with plist.open("wb") as handle:
            self_heal.plistlib.dump({
                "ProgramArguments": [
                    str(interpreter), "-m", "hermes_cli.main", "gateway",
                ],
            }, handle)
        monkeypatch.setattr(self_heal, "GATEWAY_PLIST", plist)

        assert self_heal.gateway_python() == interpreter
    finally:
        sys.modules.pop(name, None)


def test_self_heal_reports_fresh_retrieval_failure_as_evaluation_failure(
    tmp_path,
    monkeypatch,
):
    name, self_heal = load_self_heal_module()
    try:
        monkeypatch.setattr(self_heal, "HERMES_HOME", tmp_path)
        report_dir = tmp_path / "reports" / "memory-retrieval"
        report_dir.mkdir(parents=True)
        report = report_dir / "20260814-120000.md"
        report.write_text("# failed\n", encoding="utf-8")
        report.with_suffix(".json").write_text(
            json.dumps({
                "passed": False,
                "passed_count": 14,
                "case_count": 15,
                "backend_passed_count": 3,
                "backend_count": 3,
                "latency_p95_ms": 9,
                "interpreter": "/runtime/python",
            }),
            encoding="utf-8",
        )
        events = []

        self_heal.maybe_run_report_script(
            events,
            "memory-retrieval",
            "memory_retrieval_eval.py",
            dry_run=False,
        )

        assert len(events) == 1
        assert events[0].status == "evaluation_failed"
        assert events[0].notify
        assert "cases=14/15" in events[0].message
    finally:
        sys.modules.pop(name, None)


def test_self_heal_runs_retrieval_eval_with_gateway_runtime(
    tmp_path,
    monkeypatch,
):
    name, self_heal = load_self_heal_module()
    try:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        eval_script = scripts / "memory_retrieval_eval.py"
        eval_script.touch()
        report_dir = tmp_path / "reports" / "memory-retrieval"
        report_dir.mkdir(parents=True)
        old_report = report_dir / "20260812-000000.md"
        old_report.write_text("# stale\n", encoding="utf-8")
        old_time = old_report.stat().st_mtime - 48 * 3600
        os.utime(old_report, (old_time, old_time))
        interpreter = tmp_path / "gateway-python"
        interpreter.touch()
        commands = []

        def run_command(args, timeout):
            commands.append((args, timeout))
            fresh = report_dir / "20260814-130000.md"
            fresh.write_text("# fresh failure\n", encoding="utf-8")
            fresh.with_suffix(".json").write_text(
                json.dumps({
                    "passed": False,
                    "passed_count": 14,
                    "case_count": 15,
                    "backend_passed_count": 3,
                    "backend_count": 3,
                    "latency_p95_ms": 8,
                    "interpreter": str(interpreter),
                }),
                encoding="utf-8",
            )
            return 1, "fresh evaluation failed"

        monkeypatch.setattr(self_heal, "HERMES_HOME", tmp_path)
        monkeypatch.setattr(self_heal, "SCRIPTS_DIR", scripts)
        monkeypatch.setattr(self_heal, "gateway_python", lambda: interpreter)
        monkeypatch.setattr(self_heal, "run_command", run_command)
        events = []

        self_heal.maybe_run_report_script(
            events,
            "memory-retrieval",
            "memory_retrieval_eval.py",
            dry_run=False,
        )

        assert commands == [(
            [
                str(interpreter),
                str(eval_script),
                "--vault-home",
                str(tmp_path),
            ],
            180,
        )]
        assert events[-1].status == "evaluation_failed"
        assert "regenerate_failed" not in {event.status for event in events}
    finally:
        sys.modules.pop(name, None)


def test_controlled_learning_extracts_only_conservative_durable_facts():
    name, controlled = load_controlled_learning_module()
    try:
        extract = controlled.extract_durable_facts
        assert extract("请记住：我以后偏好蓝色界面")
        assert extract("我老婆叫测试配偶") == [
            ("family:spouse:测试配偶", "配偶：测试配偶。"),
        ]
        assert extract("我喜欢简洁直接的回答")
        assert extract("我现在的是微星4080，不是七彩虹") == []
        assert extract("帮我算一下 37 乘以 19") == []
        assert extract("不是记下来。你要开始改。") == []
    finally:
        sys.modules.pop(name, None)


def test_controlled_learning_blocks_non_authoritative_vault_write(
    vault,
    monkeypatch,
):
    name, controlled = load_controlled_learning_module()
    try:
        monkeypatch.setattr(controlled, "load_memory_vault", lambda: vault)
        user_file = vault.HERMES_HOME / "memories" / "USER.md"
        before_exists = user_file.exists()
        before = (
            user_file.read_text(encoding="utf-8")
            if before_exists
            else ""
        )
        fact = controlled.Fact(
            key="preference:test",
            content="用户长期偏好蓝色界面",
            support=2,
            candidate_ids=["session-a", "session-b"],
            source_messages=["a", "b"],
        )

        outcome, reason = controlled.stage_fact(fact, dry_run=False)

        assert outcome == "blocked_non_authoritative"
        assert "may not write durable Vault memory" in reason
        assert vault.pending_records() == []
        assert user_file.exists() == before_exists
        if before_exists:
            assert user_file.read_text(encoding="utf-8") == before
    finally:
        sys.modules.pop(name, None)


def test_controlled_learning_never_stages_high_risk_candidate():
    name, controlled = load_controlled_learning_module()
    try:
        facts, decisions = controlled.build_facts([{
            "id": "candidate",
            "status": "stage_user_memory",
            "risk": "high",
            "score": 8,
            "source": "weixin",
            "text": "记住：用户当前血压为140/95",
        }])

        assert facts == []
        assert decisions[0].outcome == "queued_policy"
    finally:
        sys.modules.pop(name, None)


def test_existing_pending_candidate_is_reclassified_by_current_policy(
    vault,
    monkeypatch,
):
    first, created = vault.propose_record(
        "用户长期偏好蓝色界面",
        source="test",
        evidence_session="session-a",
    )
    assert created
    assert first["risk"] == "low"
    monkeypatch.setattr(vault, "is_high_risk_memory", lambda body, topic: True)

    second, created = vault.propose_record(
        "用户长期偏好蓝色界面",
        source="test",
        evidence_session="session-b",
    )

    assert not created
    assert second["id"] == first["id"]
    assert second["risk"] == "high"
    assert second["review_status"] == "needs_user_review"
    assert vault.evidence_count(second["id"]) == 2


def test_governor_rechecks_risk_before_auto_promotion(
    vault,
    tmp_path,
    monkeypatch,
):
    candidate, _ = vault.propose_record(
        "用户长期偏好蓝色界面",
        source="test",
        evidence_session="session-a",
    )
    vault.propose_record(
        "用户长期偏好蓝色界面",
        source="test",
        evidence_session="session-b",
    )
    name, governor = load_memory_governor_module()
    try:
        monkeypatch.setattr(governor, "load_memory_vault", lambda: vault)
        monkeypatch.setattr(
            governor,
            "STATE_FILE",
            tmp_path / "governor-state.json",
        )
        monkeypatch.setattr(
            vault,
            "is_high_risk_memory",
            lambda body, topic: True,
        )

        payload = governor.run_governance(dry_run=False, no_sync=True)

        assert payload["auto_promoted"] == []
        assert [row["id"] for row in payload["needs_user_review"]] == [
            candidate["id"],
        ]
        stored = {
            row["id"]: row
            for row in vault.read_jsonl(vault.RECORDS_PATH)
        }
        assert stored[candidate["id"]]["status"] == "pending"
        assert stored[candidate["id"]]["risk"] == "high"
        assert (
            stored[candidate["id"]]["review_status"]
            == "needs_user_review"
        )
    finally:
        sys.modules.pop(name, None)


def test_learning_actions_routes_durable_facts_to_vault_and_keeps_risk():
    name, actions = load_learning_actions_module()
    try:
        base = {
            "session_id": "session",
            "timestamp": 1,
            "source": "weixin",
            "title": "",
            "role": "user",
            "tool_name": "",
        }
        preference = actions.classify_user_message(
            {**base, "content": "我以后都喜欢直接给结论"},
            set(),
        )
        health = actions.classify_user_message(
            {**base, "content": "记住：我的剂量改成5mg"},
            set(),
        )
        correction = actions.classify_user_message(
            {**base, "content": "我现在的是微星4080，不是七彩虹"},
            set(),
        )

        assert preference is not None
        assert preference.status == "stage_user_memory"
        assert preference.target == "Vault candidate"
        assert health is not None
        assert health.status == "manual_review"
        assert health.risk == "high"
        assert correction is not None
        assert correction.status == "manual_review"
        assert correction.target == "Vault update candidate"
    finally:
        sys.modules.pop(name, None)


def test_self_heal_verifies_controlled_learning_vault_target(
    tmp_path,
    monkeypatch,
):
    name, self_heal = load_self_heal_module()
    try:
        monkeypatch.setattr(self_heal, "HERMES_HOME", tmp_path)
        report_dir = tmp_path / "reports" / "controlled-learning"
        report_dir.mkdir(parents=True)
        (report_dir / "20260726-000000.json").write_text(
            json.dumps({
                "target": "vault",
                "decisions": [{"outcome": "staged"}],
            }),
            encoding="utf-8",
        )
        events = []

        self_heal.check_controlled_learning_report(events)

        assert len(events) == 1
        assert events[0].status == "vault_target_verified"
        assert not events[0].notify
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize(
    ("dry_run", "expected_saves"),
    [(True, 0), (False, 1)],
)
def test_self_heal_dry_run_does_not_persist_state(
    tmp_path,
    monkeypatch,
    dry_run,
    expected_saves,
):
    name, self_heal = load_self_heal_module()
    try:
        state = {"marker": "unchanged"}
        monkeypatch.setattr(self_heal, "load_state", lambda: state)
        monkeypatch.setattr(self_heal, "memory_provider", lambda: "vault")
        for function_name in (
            "check_telegram_gateway",
            "check_memory_config",
            "enforce_controlled_write_gates",
            "check_builtin_memory_capacity",
            "check_astrology_semantics",
            "check_capacity",
            "check_reports",
            "check_memory_governance_report",
            "check_controlled_learning_report",
            "prune_generated_reports",
            "check_script_health",
            "check_cron_jobs",
            "check_cron_prompt_memory_writes",
            "update_known_issues",
        ):
            monkeypatch.setattr(
                self_heal,
                function_name,
                lambda *args, **kwargs: None,
            )
        monkeypatch.setattr(self_heal, "load_jobs", lambda: [])
        saves = []
        monkeypatch.setattr(
            self_heal,
            "save_state",
            lambda payload: saves.append(payload),
        )
        monkeypatch.setattr(
            self_heal,
            "write_report",
            lambda events, payload: (
                tmp_path / "report.md",
                tmp_path / "report.json",
            ),
        )
        monkeypatch.setattr(
            self_heal,
            "notification_text",
            lambda events, path: "",
        )
        argv = ["hermes_self_heal.py"]
        if dry_run:
            argv.append("--dry-run")
        monkeypatch.setattr(self_heal.sys, "argv", argv)

        assert self_heal.main() == 0
        assert len(saves) == expected_saves
    finally:
        sys.modules.pop(name, None)


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


def test_explicit_conflicting_request_is_not_promoted_as_second_active_truth():
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
                "risk": "high",
                "governance_action": "needs_user_review",
                "matched_id": "weak-similar-memory",
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
        assert provider._vault.promotions == []
    finally:
        sys.modules.pop(name, None)


def test_direct_remember_does_not_activate_a_merge_candidate():
    name, plugin = load_vault_provider_module()
    try:
        assert not plugin.VaultMemoryProvider._is_direct_add({
            "status": "pending",
            "risk": "high",
            "governance_action": "merge",
            "matched_id": "active-memory",
        })
    finally:
        sys.modules.pop(name, None)


def test_vault_remember_activates_direct_low_risk_fact(vault):
    name, plugin = load_vault_provider_module()
    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = vault
        provider._session_id = "turn-session"

        payload = json.loads(provider.handle_tool_call(
            "vault_remember",
            {"content": "用户长期偏好蓝色界面"},
        ))

        assert payload["status"] == "active"
        assert payload["result"] == "Durable memory stored."
        active = vault.active_records_by_id()
        assert payload["id"] in active
    finally:
        sys.modules.pop(name, None)


def test_vault_remember_activates_direct_sensitive_fact(vault):
    name, plugin = load_vault_provider_module()
    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = vault
        provider._session_id = "turn-session"

        payload = json.loads(provider.handle_tool_call(
            "vault_remember",
            {
                "content": (
                    "用户截至2026-07-23持有测试资产1800份，"
                    "参考成本33.740测试币。"
                )
            },
        ))

        assert payload["status"] == "active"
        assert payload["risk"] == "high"
        assert payload["result"] == "Durable memory stored."
        assert payload["id"] in vault.active_records_by_id()
        record = vault.active_records_by_id()[payload["id"]]
        assert record["governance_action"] == "add"
    finally:
        sys.modules.pop(name, None)


def test_background_sensitive_candidate_stays_pending(vault):
    name, plugin = load_vault_provider_module()
    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = vault
        provider._session_id = "turn-session"

        result = provider.propose_candidate(
            "用户有高血压，当前血压为135/90",
            {
                "source_type": "user_fact",
                "domain": "medical",
                "source_session_id": "source-session",
            },
        )

        assert result is not None
        assert result["status"] == "pending"
        assert result["risk"] == "high"
        assert result["id"] not in vault.active_records_by_id()
    finally:
        sys.modules.pop(name, None)


def test_vault_update_immediately_versions_low_risk_correction(vault):
    old, _ = vault.add_record("用户长期偏好红色界面", topic="profile")
    name, plugin = load_vault_provider_module()
    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = vault
        provider._session_id = "turn-session"

        first = json.loads(provider.handle_tool_call(
            "vault_update",
            {
                "memory_id": old["id"],
                "content": "用户长期偏好蓝色界面",
            },
        ))
        second = json.loads(provider.handle_tool_call(
            "vault_update",
            {
                "memory_id": old["id"],
                "content": "用户长期偏好蓝色界面",
            },
        ))

        assert first["status"] == "active"
        assert first["replaces"] == old["id"]
        assert second["id"] == first["id"]
        records = vault.read_jsonl(vault.RECORDS_PATH)
        assert len(records) == 2
        statuses = {record["id"]: record["status"] for record in records}
        assert statuses[old["id"]] == "superseded"
        assert statuses[first["id"]] == "active"
    finally:
        sys.modules.pop(name, None)


def test_vault_update_immediately_versions_direct_sensitive_correction(vault):
    old, _ = vault.add_record(
        "用户截至2026-07-23持有测试资产1800份，参考成本33.740测试币。",
        topic="投资持仓",
    )
    name, plugin = load_vault_provider_module()
    try:
        provider = plugin.VaultMemoryProvider()
        provider._vault = vault
        provider._session_id = "turn-session"

        payload = json.loads(provider.handle_tool_call(
            "vault_update",
            {
                "memory_id": old["id"],
                "content": (
                    "用户截至2026-08-01持有测试资产2000份，"
                    "平均成本33.500测试币。"
                ),
            },
        ))

        assert payload["status"] == "active"
        assert payload["result"] == "Durable memory updated."
        records = {
            record["id"]: record
            for record in vault.read_jsonl(vault.RECORDS_PATH)
        }
        assert records[old["id"]]["status"] == "superseded"
        assert records[payload["id"]]["status"] == "active"
        assert records[payload["id"]]["risk"] == "high"
        assert "2026-07-23" in records[old["id"]]["body"]
        assert "2026-08-01" in records[payload["id"]]["body"]
    finally:
        sys.modules.pop(name, None)
