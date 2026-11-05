"""診斷的設定回退與健康狀態；只隔離外部探測，不替換診斷或狀態函式。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.routers import system


@pytest.fixture
def diagnostics_env(client, monkeypatch):
    settings = system.get_settings().model_copy(
        update={
            "hybrid_model": "",
            "hybrid_fallback_model": "",
            "api_model": "",
            "ortho2cad_model": "",
            "lora_model": "",
            "lora_enabled": False,
            "hybrid_base_url": "http://primary.invalid/v1",
            "hybrid_fallback_base_url": "",
            "ortho2cad_base_url": "http://cad.invalid/v1",
        }
    )
    monkeypatch.setattr(system, "get_settings", lambda: settings)
    state = SimpleNamespace(settings=settings, hybrid_ok=True, pings=[])

    async def ping_ollama(base_url, simulate_outage=True):
        state.pings.append((base_url, simulate_outage))
        ok = state.hybrid_ok if simulate_outage else True
        return ok, f"probe:{base_url}"

    monkeypatch.setattr(system.providers, "ping_ollama", ping_ollama)
    state.cloud_status = Mock(return_value=(False, "cloud disabled in test"))
    monkeypatch.setattr(system.providers, "cloud_status", state.cloud_status)
    state.get_provider = Mock(side_effect=AssertionError("診斷不得執行模型或付費 API"))
    monkeypatch.setattr(system.providers, "get_provider", state.get_provider)
    monkeypatch.setitem(system.providers.OUTAGE, "enabled", False)

    # 與 J-9 environment-addendum 相同的外部 metadata 邊界；保留真正的 memory status。
    memory = system.memory_guard
    monkeypatch.setattr(memory, "_ollama_size_mb", lambda root, name: 4000)
    monkeypatch.setattr(memory, "_ollama_probe", lambda: None)
    monkeypatch.setattr(memory, "_ortho_probe", lambda: None)
    monkeypatch.setattr(memory, "_timefold_probe", lambda: None)

    state.logs = SimpleNamespace(ping=Mock(return_value=True))
    state.recent = {}
    for name in ("recent_chats", "recent_cad", "recent_sql", "recent_routes"):
        query = Mock(return_value=[{"kind": name}])
        setattr(state.logs, name, query)
        state.recent[name] = query
    monkeypatch.setattr(system, "get_logs_repo", lambda: state.logs)
    yield state
    state.get_provider.assert_not_called()


@pytest.mark.parametrize("configured_model", ["", "explicit-diagnostics-model"])
@pytest.mark.parametrize(
    ("setting_name", "strategy"),
    [
        ("hybrid_model", "hybrid"),
        ("hybrid_fallback_model", "hybrid_fallback"),
        ("api_model", "api_nokb"),
        ("api_model", "api_kb"),
        ("ortho2cad_model", "ortho2cad"),
        ("lora_model", "lora"),
    ],
)
def test_diagnostics_model_defaults_and_overrides(
    client, diagnostics_env, setting_name, strategy, configured_model
):
    default_model = system.get_models_config().strategies[strategy].default_model
    assert default_model != "explicit-diagnostics-model"
    setattr(diagnostics_env.settings, setting_name, configured_model)

    response = client.get("/api/v1/admin/diagnostics")

    assert response.status_code == 200, response.text
    status = response.json()["strategies"][strategy]
    expected = default_model if configured_model == "" else configured_model
    assert status["model"] == expected
    if strategy in ("api_nokb", "api_kb"):
        assert status["available"] is False
        assert status["detail"] == "cloud disabled in test"
    if strategy == "lora":
        assert status["available"] is False
        assert status["detail"] == "選做：插槽已保留，未啟用"
    diagnostics_env.cloud_status.assert_called_once_with()


@pytest.mark.parametrize(
    ("fallback_url", "expected_url"),
    [
        ("", "http://primary.invalid/v1"),
        ("http://fallback.invalid/v1", "http://fallback.invalid/v1"),
    ],
)
def test_diagnostics_real_mode_probes_selected_fallback_url(
    client, diagnostics_env, fallback_url, expected_url
):
    settings = diagnostics_env.settings
    settings.llm_mode = "real"
    settings.hybrid_fallback_base_url = fallback_url

    response = client.get("/api/v1/admin/diagnostics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["llm_mode"] == "real"
    assert diagnostics_env.pings == [
        ("http://primary.invalid/v1", True),
        (expected_url, False),
        ("http://cad.invalid/v1", False),
    ]
    assert body["strategies"]["hybrid_fallback"]["available"] is True
    assert body["strategies"]["hybrid_fallback"]["detail"] == (
        f"{expected_url}（probe:{expected_url}）"
    )


@pytest.mark.parametrize(
    ("db_ok", "index_stale", "hybrid_ok", "expected_status"),
    [
        pytest.param(True, False, True, "ok", id="healthy"),
        pytest.param(False, False, True, "degraded", id="db-down"),
        pytest.param(True, True, True, "degraded", id="index-stale"),
        pytest.param(True, False, False, "degraded", id="hybrid-down"),
    ],
)
def test_diagnostics_and_status_require_all_health_checks(
    client, diagnostics_env, monkeypatch, db_ok, index_stale, hybrid_ok, expected_status
):
    state = diagnostics_env
    state.settings.llm_mode = "real"
    state.hybrid_ok = hybrid_ok
    state.logs.ping.return_value = db_ok
    if not db_ok:
        for query in state.recent.values():
            query.side_effect = AssertionError("DB down 時不得查詢最近紀錄")
    if index_stale:
        store = system.get_store()
        monkeypatch.setattr(store, "manifest", {**store.manifest, "kb_hash": "stale-test-hash"})

    response = client.get("/api/v1/admin/diagnostics")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == expected_status
    assert body["db"] is db_ok
    assert body["index_consistent"] is (not index_stale)
    assert body["index_problems"] == (
        ["kb/ 內容已變更但尚未重建索引（請執行 make index）"] if index_stale else []
    )
    assert body["strategies"]["hybrid"]["available"] is hybrid_ok
    for name, query in state.recent.items():
        if db_ok:
            assert body[name] == [{"kind": name}]
            query.assert_called_once_with(10)
        else:
            assert body[name] == []
            query.assert_not_called()
        query.reset_mock()

    response = client.get("/api/v1/status")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == expected_status
    assert response.json()["strategies"]["hybrid"]["available"] is hybrid_ok
    for query in state.recent.values():
        query.assert_not_called()
    assert state.logs.ping.call_count == 2
