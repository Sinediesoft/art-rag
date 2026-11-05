"""針對 system API 公開輸入、輸出與依賴邊界的回歸測試。"""

import json
from datetime import datetime as RealDateTime
from datetime import timedelta, timezone

import pytest
from conftest import as_account
from test_system_diagnostics import diagnostics_env as diagnostics_env


@pytest.fixture(autouse=True)
def restore_manager(client):
    yield
    as_account(client, "manager")


def test_security_logs_translate_taiwan_midnight_and_report_stage_counts(client, monkeypatch):
    from app.api.routers import system as routes

    class TaiwanMidnight(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 29, 0, 5, tzinfo=timezone(timedelta(hours=8)))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    calls = {"counts": [], "recent": []}
    response_counts = {1: 11, 2: 22, 4: 44, 7: 77}

    class Logs:
        def count_security(self, since):
            calls["counts"].append(since)
            return response_counts

        def recent_security(self, limit):
            calls["recent"].append(limit)
            return []

    monkeypatch.setattr(routes, "datetime", TaiwanMidnight)
    monkeypatch.setattr(routes, "get_logs_repo", Logs)
    as_account(client, "manager")

    result = client.get("/api/v1/security/logs").json()
    assert calls == {"counts": ["2026-10-28T16:00:00+00:00"], "recent": [20]}
    assert result["today"] == {"rbac": 11, "guard": 22, "post": 44, "output": 77}

    response_counts.clear()
    assert client.get("/api/v1/security/logs").json()["today"] == {
        "rbac": 0,
        "guard": 0,
        "post": 0,
        "output": 0,
    }


@pytest.mark.parametrize("limit", [1, 200])
def test_security_logs_forward_valid_limits(client, monkeypatch, limit):
    from app.api.routers import system as routes

    seen = []
    monkeypatch.setattr(
        routes,
        "get_logs_repo",
        lambda: type(
            "Logs",
            (),
            {
                "count_security": lambda self, since: {},
                "recent_security": lambda self, value: seen.append(value) or [],
            },
        )(),
    )
    as_account(client, "manager")
    assert client.get(f"/api/v1/security/logs?limit={limit}").status_code == 200
    assert seen == [limit]


@pytest.mark.parametrize("limit", [0, 201])
def test_security_logs_reject_limits_outside_api_bounds(client, limit):
    as_account(client, "manager")
    assert client.get(f"/api/v1/security/logs?limit={limit}").status_code == 422


def test_audit_defaults_bounds_and_passes_query_limit_to_both_repositories(client, monkeypatch):
    from app.api.routers import system as routes

    calls = {"audit": [], "changes": [], "reads": [], "flush": 0}

    class Production:
        def audit(self, limit):
            calls["audit"].append(limit)
            return []

        def changes(self, *, limit):
            calls["changes"].append(limit)
            return []

    class Logs:
        def recent_access(self, limit):
            calls["reads"].append(limit)
            return []

    monkeypatch.setattr(routes, "get_production_repo", Production)
    monkeypatch.setattr(routes, "get_logs_repo", Logs)
    monkeypatch.setattr(
        routes.cad_cache, "flush_audit", lambda: calls.__setitem__("flush", calls["flush"] + 1)
    )
    as_account(client, "manager")

    assert client.get("/api/v1/audit").status_code == 200
    assert client.get("/api/v1/audit?limit=1").status_code == 200
    assert client.get("/api/v1/audit?limit=200").status_code == 200
    assert calls == {
        "audit": [30, 1, 200],
        "changes": [10, 10, 10],
        "reads": [30, 1, 200],
        "flush": 3,
    }
    assert client.get("/api/v1/audit?limit=0").status_code == 422
    assert client.get("/api/v1/audit?limit=201").status_code == 422


def test_eval_run_endpoints_sort_filter_and_scrub_only_route_rows(client, monkeypatch, tmp_path):
    from app.api.routers import system as routes

    run_dir = tmp_path / "eval" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "2026-10-01.json").write_text(
        json.dumps(
            {
                "run_id": "ordinary",
                "created_at": "2026-10-01T00:00:00Z",
                "kb_version": "kb",
                "prompt_version": "p1",
                "summary": {},
                "by_strategy": {},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "ignore-generation-accounts.json").write_text("{}", encoding="utf-8")
    (run_dir / "new-cad.json").write_text('{"run_id":"cad"}', encoding="utf-8")
    (run_dir / "new-sql.json").write_text('{"run_id":"sql"}', encoding="utf-8")
    for index in range(12):
        (run_dir / f"r{index:02}-route.json").write_text(
            json.dumps(
                {
                    "run_id": f"route-{index:02}",
                    "engines": [
                        {"name": "local", "accuracy": 0.9, "rows": [{"question": "private"}]}
                    ],
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(routes, "REPO_ROOT", tmp_path)
    as_account(client, "manager")

    ordinary = client.get("/api/v1/eval/runs").json()["runs"]
    assert [row["run_id"] for row in ordinary] == ["ordinary"]
    cad = client.get("/api/v1/eval/cad-runs").json()["runs"]
    sql = client.get("/api/v1/eval/sql-runs").json()["runs"]
    assert [row["run_id"] for row in cad] == ["cad"]
    assert [row["run_id"] for row in sql] == ["sql"]

    route_runs = client.get("/api/v1/eval/route-runs").json()["runs"]
    assert [row["run_id"] for row in route_runs] == [f"route-{i:02}" for i in range(11, 1, -1)]
    assert all(
        engine == {"name": "local", "accuracy": 0.9}
        for row in route_runs
        for engine in row["engines"]
    )


def test_demo_release_and_outage_success_paths(client, monkeypatch):
    from app.api.routers import system as routes

    guard = routes.memory_guard.guard
    old_models = set(guard.current_models)
    released = []
    # release 的回應會讀 guard.status()，隔離其中的本機模型服務探測。
    monkeypatch.setattr(routes.memory_guard, "_models", lambda: [])
    monkeypatch.setattr(
        guard,
        "check",
        lambda reason, models, force=False: released.append((reason, set(models), force)) or None,
    )
    previous_outage = routes.providers.OUTAGE["enabled"]
    as_account(client, "manager")
    try:
        result = client.post("/api/v1/admin/memory/release")
        assert result.status_code == 200, result.text
        assert result.json()["event"] is None
        assert released == [("手動釋放", old_models, True)]

        assert client.post("/api/v1/admin/outage", json={"enabled": True}).json() == {"ok": True}
        assert routes.providers.OUTAGE["enabled"] is True
        assert client.post("/api/v1/admin/outage", json={"enabled": False}).json() == {"ok": True}
        assert routes.providers.OUTAGE["enabled"] is False
    finally:
        routes.providers.OUTAGE["enabled"] = previous_outage


@pytest.mark.parametrize(
    ("mode", "db_ok", "problems", "ping_ok", "expected_status", "expected_ready"),
    [
        ("real", True, ["index missing"], False, "degraded", False),
        ("real", False, [], True, "degraded", False),
        ("mock", True, [], True, "ok", True),
    ],
)
def test_health_reports_db_index_and_real_mode_ping_independently(
    client, monkeypatch, mode, db_ok, problems, ping_ok, expected_status, expected_ready
):
    from types import SimpleNamespace

    from app.api.routers import system as routes

    ping_calls = []

    class Logs:
        def ping(self):
            return db_ok

    async def ping_ollama(url):
        ping_calls.append(url)
        return ping_ok, "controlled probe"

    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(
            llm_mode=mode, demo_controls=False, hybrid_base_url="http://localhost:11434/v1"
        ),
    )
    monkeypatch.setattr(routes, "_index_problems", lambda: problems)
    monkeypatch.setattr(routes, "get_logs_repo", Logs)
    monkeypatch.setattr(routes.providers, "ping_ollama", ping_ollama)
    monkeypatch.setattr(routes, "kb_version", lambda: "test-kb")

    result = client.get("/api/v1/health")
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == expected_status and body["ready"] is expected_ready
    assert body["db"] is db_ok
    assert body["index_consistent"] is (not problems)
    assert body["kb_version"] == "test-kb"
    assert ping_calls == (["http://localhost:11434/v1"] if mode == "real" else [])
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("demo_enabled", [True, False])
def test_health_and_status_report_demo_warning_for_server_switch(
    client, diagnostics_env, monkeypatch, demo_enabled
):
    from app.api.routers import system as routes

    as_account(client, "manager")
    settings = diagnostics_env.settings
    settings.demo_controls = demo_enabled
    monkeypatch.setattr(routes.identity, "get_settings", lambda: settings)
    expected_warning = routes.DEMO_WARNING if demo_enabled else None

    for endpoint in ("/api/v1/health", "/api/v1/status"):
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["demo_controls"] is demo_enabled
        assert body["demo_warning"] == expected_warning


@pytest.mark.parametrize("hybrid_ok", [True, False])
def test_status_availability_detail_matches_probed_service(client, diagnostics_env, hybrid_ok):
    diagnostics_env.settings.llm_mode = "real"
    diagnostics_env.hybrid_ok = hybrid_ok

    response = client.get("/api/v1/status")

    assert response.status_code == 200, response.text
    hybrid = response.json()["strategies"]["hybrid"]
    assert hybrid["available"] is hybrid_ok
    assert hybrid["detail"] == ("可以使用" if hybrid_ok else "目前無法使用")


@pytest.mark.parametrize("ortho_ok", [True, False])
def test_diagnostics_ortho2cad_start_hint_tracks_service_failure(
    client, diagnostics_env, monkeypatch, ortho_ok
):
    from app.api.routers import system as routes

    settings = diagnostics_env.settings
    settings.llm_mode = "real"

    async def ping_ollama(base_url, simulate_outage=True):
        return (ortho_ok if base_url == settings.ortho2cad_base_url else True), "probe result"

    monkeypatch.setattr(routes.providers, "ping_ollama", ping_ollama)
    response = client.get("/api/v1/admin/diagnostics")

    assert response.status_code == 200, response.text
    ortho = response.json()["strategies"]["ortho2cad"]
    assert ortho["available"] is ortho_ok
    assert ("請執行 make ortho2cad" in ortho["detail"]) is (not ortho_ok)


@pytest.mark.parametrize("inventory_ok", [True, False])
def test_diagnostics_inventory_preserves_public_fields(
    client, diagnostics_env, monkeypatch, inventory_ok
):
    from types import SimpleNamespace

    from app.api.routers import system as routes

    inventory = SimpleNamespace(
        ping=lambda: inventory_ok,
        as_of="2026-10-29",
        manifest={"tables": {"parts": {"rows": 7}}},
        problems=[] if inventory_ok else ["inventory snapshot unavailable"],
    )
    monkeypatch.setattr(routes, "get_inventory_repo", lambda: inventory)

    response = client.get("/api/v1/admin/diagnostics")

    assert response.status_code == 200, response.text
    assert response.json()["inventory"] == {
        "ok": inventory_ok,
        "as_of": "2026-10-29",
        "tables": {"parts": {"rows": 7}},
        "problems": inventory.problems,
    }
