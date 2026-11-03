"""端點盤點與相容對照（TASK-20261013-001，AC-01、AC-02）。

`LEGACY` 是重構之前（71d707f）openapi.json 裡全部 75 個（方法, 路徑）：重構後一個都不能少
（保留舊的相容入口，不靜默移除功能）。`NEW` 是這次新增的端點。依領域拆分的 router 也要涵蓋全部。
"""

from fastapi.routing import APIRoute

from app.api.routes import router
from app.main import app

LEGACY = [
    ("GET", "/api/v1/admin/diagnostics"),
    ("POST", "/api/v1/admin/memory/release"),
    ("POST", "/api/v1/admin/outage"),
    ("POST", "/api/v1/admin/production/reset"),
    ("POST", "/api/v1/agent/route"),
    ("GET", "/api/v1/approvals"),
    ("POST", "/api/v1/approvals/{ap_no}/approve"),
    ("POST", "/api/v1/approvals/{ap_no}/return"),
    ("GET", "/api/v1/artworks"),
    ("GET", "/api/v1/artworks/{artwork_id}"),
    ("GET", "/api/v1/artworks/{artwork_id}/colormap.png"),
    ("GET", "/api/v1/artworks/{artwork_id}/colors"),
    ("GET", "/api/v1/artworks/{artwork_id}/image"),
    ("POST", "/api/v1/artworks/{artwork_id}/region-drafts"),
    ("GET", "/api/v1/audit"),
    ("GET", "/api/v1/auth/accounts"),
    ("POST", "/api/v1/auth/switch"),
    ("POST", "/api/v1/batch/identify"),
    ("GET", "/api/v1/cad/jobs/{job_id}"),
    ("GET", "/api/v1/cad/jobs/{job_id}/{name}"),
    ("POST", "/api/v1/cad/reconstruct"),
    ("POST", "/api/v1/changes/preview"),
    ("POST", "/api/v1/changes/{pending_id}/commit"),
    ("POST", "/api/v1/changes/{pending_id}/request-approval"),
    ("POST", "/api/v1/chat"),
    ("GET", "/api/v1/compare/items"),
    ("POST", "/api/v1/compare/summary"),
    ("GET", "/api/v1/eval/cad-runs"),
    ("GET", "/api/v1/eval/route-runs"),
    ("GET", "/api/v1/eval/runs"),
    ("GET", "/api/v1/eval/sql-runs"),
    ("POST", "/api/v1/exports"),
    ("POST", "/api/v1/feedback"),
    ("GET", "/api/v1/health"),
    ("POST", "/api/v1/images"),
    ("GET", "/api/v1/images/{image_id}"),
    ("GET", "/api/v1/images/{image_id}/align"),
    ("GET", "/api/v1/images/{image_id}/align.png"),
    ("GET", "/api/v1/images/{image_id}/colormap.png"),
    ("GET", "/api/v1/images/{image_id}/colors"),
    ("GET", "/api/v1/images/{image_id}/style"),
    ("POST", "/api/v1/intake"),
    ("DELETE", "/api/v1/intake/{draft_id}"),
    ("GET", "/api/v1/intake/{draft_id}"),
    ("PUT", "/api/v1/intake/{draft_id}"),
    ("POST", "/api/v1/intake/{draft_id}/commit"),
    ("GET", "/api/v1/intake/{draft_id}/{name}"),
    ("POST", "/api/v1/inventory/ask"),
    ("GET", "/api/v1/inventory/overview"),
    ("GET", "/api/v1/inventory/parts/{part_id}"),
    ("GET", "/api/v1/inventory/schema"),
    ("GET", "/api/v1/memory"),
    ("GET", "/api/v1/parts"),
    ("GET", "/api/v1/parts/{part_id}"),
    ("GET", "/api/v1/parts/{part_id}/drawing"),
    ("GET", "/api/v1/parts/{part_id}/model.{ext}"),
    ("GET", "/api/v1/parts/{part_id}/reconstructions"),
    ("GET", "/api/v1/production/overview"),
    ("GET", "/api/v1/production/parts/{part_id}"),
    ("POST", "/api/v1/production/work-orders"),
    ("DELETE", "/api/v1/production/work-orders/{wo_no}"),
    ("GET", "/api/v1/region-drafts"),
    ("DELETE", "/api/v1/region-drafts/{draft_id}"),
    ("POST", "/api/v1/region-drafts/{draft_id}/commit"),
    ("POST", "/api/v1/region-drafts/{draft_id}/return"),
    ("GET", "/api/v1/schedule/runs/{run_id}"),
    ("POST", "/api/v1/schedule/solve"),
    ("POST", "/api/v1/schedule/stop"),
    ("POST", "/api/v1/search/any"),
    ("POST", "/api/v1/search/drawing"),
    ("POST", "/api/v1/search/image"),
    ("GET", "/api/v1/search/parts"),
    ("GET", "/api/v1/search/text"),
    ("GET", "/api/v1/security/logs"),
    ("GET", "/api/v1/status"),
]

NEW = [
    ("POST", "/api/v1/cad/jobs"),
    ("POST", "/api/v1/schedule/jobs"),
    ("GET", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/jobs/{job_id}/events"),
    ("POST", "/api/v1/jobs/{job_id}/cancel"),
    ("GET", "/api/v1/jobs/{job_id}/artifacts/{name}"),
    ("PUT", "/api/v1/images/{image_id}/acl"),
    # 3D 重建結果快取（TASK-20261014-002，docs/adr/042）：作廢、採用
    ("POST", "/api/v1/cad/jobs/{job_id}/cache/reject"),
    ("POST", "/api/v1/cad/jobs/{job_id}/cache/adopt"),
]


def operations() -> set[tuple[str, str]]:
    spec = app.openapi()
    return {
        (m.upper(), p)
        for p, item in spec["paths"].items()
        for m in item
        if m in ("get", "post", "put", "delete", "patch")
    }


def test_every_legacy_endpoint_is_still_served():
    missing = [op for op in LEGACY if op not in operations()]
    assert missing == []


def test_new_endpoints_exist_and_nothing_else_appeared_unannounced():
    ops = operations()
    assert [op for op in NEW if op not in ops] == []
    assert ops - set(LEGACY) == set(NEW)


def test_routes_are_split_into_domain_routers_with_one_api_prefix():
    # 全部在同一個 /api/v1 入口；各領域的 router 檔都有路由
    from app.api.routers import (
        agent,
        artworks,
        auth,
        cad,
        changes,
        chat,
        images,
        inventory,
        jobs,
        parts,
        production,
        system,
        tools,
    )  # fmt: skip

    paths = {r.path for r in router.routes if isinstance(r, APIRoute)}
    assert all(p.startswith("/api/v1") for p in paths) or not paths
    for mod in (agent, artworks, auth, cad, changes, chat, images, inventory, jobs, parts,
                production, system, tools):  # fmt: skip
        assert any(isinstance(r, APIRoute) for r in mod.router.routes), mod.__name__
