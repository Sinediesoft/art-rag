"""現有 8 個帳號、6 種角色的權限矩陣（TASK-20261013-001，AC-05）。

期望值是依任務與 shared/access.yaml 的說明手寫的獨立清單，不是從被測的程式推出來的：
重構（router 拆分、共用安全上下文、背景工作）之後，ops、領域、機密等級、部門、修改範圍與管理檢視
都要跟原規則一樣；主管不自動取得倉管或生管的操作。
"""

import pytest
from conftest import DEFAULT_ACCOUNT, as_account

from app.repositories.index_store import get_store
from app.services import identity, security

ALL_PARTS = {f"mfg-00{i}" for i in range(1, 7)}

ALL = {"art", "mfg", "factory"}
PUBLIC, INTERNAL, SECRET = {"公開"}, {"公開", "內部"}, {"公開", "內部", "機密"}
STOCK = {"stock_adjust", "stock_transfer", "stock_scrap", "stock_status"}
PLANNER = {"wo_create", "wo_update_due", "wo_cancel", "schedule_run", "demo_reset"}
# 帳號 → (角色, ops, 領域, 機密等級, 看得到的圖紙)
EXPECTED = {
    "guest": ("guest", set(), {"art"}, PUBLIC, set()),
    "wh1": ("warehouse", STOCK, ALL, SECRET, ALL_PARTS),
    "wh2": ("warehouse", STOCK, ALL, SECRET, ALL_PARTS),
    "sales_a": ("sales", {"so_update"}, ALL, INTERNAL, {"mfg-004", "mfg-005"}),
    "sales_b": ("sales", {"so_update"}, ALL, INTERNAL, {"mfg-004", "mfg-005"}),
    "planner": ("planner", PLANNER, ALL, SECRET, ALL_PARTS),
    "manager": ("manager", {"approve", "demo_reset", "kb_intake"}, ALL, SECRET, ALL_PARTS),
    "artist": ("artist", {"kb_annotate"}, {"art"}, PUBLIC, set()),
}
VIEWS = {"manager": {"diagnostics", "security_logs", "audit"}}


def test_there_are_exactly_the_eight_accounts_and_six_roles():
    accounts = identity.accounts()
    assert set(accounts) == set(EXPECTED)
    assert {a.role for a in accounts.values()} == {
        "guest", "warehouse", "sales", "planner", "manager", "artist"
    }  # fmt: skip


@pytest.mark.parametrize("account_id", list(EXPECTED))
def test_ops_domains_levels_and_visible_parts_match_the_original_rules(account_id):
    role, ops, domains, levels, parts = EXPECTED[account_id]
    a = identity.get_account(account_id)
    assert a.role == role and set(a.ops) == ops
    assert set(a.domains) == domains and set(a.levels) == levels
    assert identity.visible_part_ids(a, get_store().parts) == parts
    assert set(a.views) == VIEWS.get(account_id, set())


def test_manager_has_no_other_roles_operations():
    manager = identity.get_account("manager")
    for op in (PLANNER - {"demo_reset"}) | STOCK | {"so_update", "kb_annotate"}:
        assert not manager.can(op), op
    ctx = security.SecurityContext(manager, "t", None, "r")
    with pytest.raises(Exception) as e:  # 主管不能開始排程
        security.schedule_check_create(ctx)
    assert getattr(e.value, "status", None) == 403


def test_background_job_policies_use_the_same_matrix():
    """建立／控制排程只有生管；3D 重建要工廠圖紙領域＋圖紙可見；讀排程要工廠資料庫領域。"""
    accounts = identity.accounts()
    can_schedule = {a for a, acc in accounts.items() if security.schedule_can_control(acc)}
    assert can_schedule == {"planner"}
    can_read_schedule = {a for a, acc in accounts.items() if security.schedule_can_read(acc)}
    assert can_read_schedule == {"wh1", "wh2", "sales_a", "sales_b", "planner", "manager"}
    factory = {"wh1", "wh2", "planner", "manager"}
    for part, expected in (("mfg-002", factory), ("mfg-004", factory | {"sales_a", "sales_b"})):
        got = set()
        for aid, acc in identity.accounts().items():
            ctx = security.SecurityContext(acc, "t", None, "r")
            try:
                security.cad_check_create(ctx, part, None)
                got.add(aid)
            except Exception:  # noqa: BLE001
                pass
        assert got == expected, part


@pytest.mark.parametrize(
    "path,method,allowed",
    [
        ("/api/v1/admin/diagnostics", "get", {"manager"}),
        ("/api/v1/security/logs", "get", {"manager"}),
        ("/api/v1/audit", "get", {"manager"}),
    ],
)
def test_management_views_are_manager_only(client, path, method, allowed):
    for aid in EXPECTED:
        as_account(client, aid)
        status = getattr(client, method)(path).status_code
        assert (status == 200) == (aid in allowed), (aid, path, status)


def test_work_orders_only_planner_and_only_own_cancel(client):
    from app.services import schedule_service

    schedule_service.reset()
    body = {"part_id": "mfg-002", "qty": 10, "due_on": "2026-10-30"}
    allowed = set()
    for aid in EXPECTED:
        as_account(client, aid)
        r = client.post("/api/v1/production/work-orders", json=body)
        if r.status_code == 201:
            allowed.add(aid)
            wo = r.json()["wo_no"]
            others = [x for x in EXPECTED if x not in (aid,)]
            for other in others:
                as_account(client, other)
                assert client.delete(f"/api/v1/production/work-orders/{wo}").status_code in (
                    403,
                    404,
                ), other
            as_account(client, aid)
            assert client.delete(f"/api/v1/production/work-orders/{wo}").status_code == 200
    assert allowed == {"planner"}
    schedule_service.reset()


def test_region_annotation_is_only_for_assigned_artworks(client):
    """只驗被擋的情況（不寫入）：沒有 kb_annotate 的角色、藝術家對沒分配給他的畫都是 403；
    分配給藝術家的畫能送出草稿的對照組在 test_region_drafts.py。"""
    body = {
        "label": "山頂灌木",
        "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.3], [0.1, 0.3]],
        "text": "這片山頂的灌木是我小時候第一次看這幅畫時最喜歡的地方，濃墨點得又密又有力。",
        "license": "CC BY 4.0",
        "attribution": "甲",
    }
    for aid in EXPECTED:
        if aid == "artist":
            continue  # 分配給他的畫會真的建立草稿，不在這裡做
        as_account(client, aid)
        r = client.post("/api/v1/artworks/npm-000001/region-drafts", json=body)
        assert r.status_code == 403, aid
    as_account(client, "artist")
    r = client.post("/api/v1/artworks/aic-27992/region-drafts", json=body)
    assert r.status_code == 403
    as_account(client, DEFAULT_ACCOUNT)
