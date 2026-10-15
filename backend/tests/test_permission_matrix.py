"""現有 5 個帳號、5 種角色的權限矩陣（TASK-20261013-001，AC-05；帳號依 docs/adr/036）。

期望值是依任務與 shared/access.yaml 的說明手寫的獨立清單，不是從被測的程式推出來的：
重構（router 拆分、共用安全上下文、背景工作）之後，ops、領域、機密等級、部門、修改範圍與管理檢視
都要跟原規則一樣。2026-10-14 併入 docs/adr/036 後：生管併入主管，主管有所有操作；
沒有該操作的帳號（一般員工、業務、倉管、藝術家）照樣 403。
"""

import pytest
from conftest import DEFAULT_ACCOUNT, as_account

from app.repositories.index_store import get_store
from app.services import identity, security

ALL_PARTS = {f"mfg-00{i}" for i in range(1, 7)}

ALL = {"art", "mfg", "factory"}
PUBLIC, INTERNAL, SECRET = {"公開"}, {"公開", "內部"}, {"公開", "內部", "機密"}
STOCK = {"stock_adjust", "stock_transfer", "stock_scrap", "stock_status"}
PRODUCTION = {"wo_create", "wo_update_due", "wo_cancel", "schedule_run"}
# 主管：所有操作（docs/adr/036）
MANAGER = STOCK | {"so_update"} | PRODUCTION | {"approve", "kb_intake", "kb_annotate", "demo_reset"}
# 帳號 → (角色, ops, 領域, 機密等級, 看得到的圖紙)
EXPECTED = {
    "guest": ("guest", set(), {"art"}, PUBLIC, set()),
    "warehouse": ("warehouse", STOCK, ALL, SECRET, ALL_PARTS),
    "sales": ("sales", {"so_update"}, ALL, INTERNAL, {"mfg-004", "mfg-005"}),
    "manager": ("manager", MANAGER, ALL, SECRET, ALL_PARTS),
    "artist": ("artist", {"kb_annotate"}, {"art"}, PUBLIC, set()),
}
VIEWS = {"manager": {"diagnostics", "security_logs", "audit", "eval"}}


def test_there_are_exactly_the_five_accounts_and_five_roles():
    accounts = identity.accounts()
    assert set(accounts) == set(EXPECTED)
    roles = {a.role for a in accounts.values()}
    assert roles == {"guest", "warehouse", "sales", "manager", "artist"}


@pytest.mark.parametrize("account_id", list(EXPECTED))
def test_ops_domains_levels_and_visible_parts_match_the_original_rules(account_id):
    role, ops, domains, levels, parts = EXPECTED[account_id]
    a = identity.get_account(account_id)
    assert a.role == role and set(a.ops) == ops
    assert set(a.domains) == domains and set(a.levels) == levels
    assert identity.visible_part_ids(a, get_store().parts) == parts
    assert set(a.views) == VIEWS.get(account_id, set())


def test_manager_has_every_operation_and_others_without_the_op_are_denied():
    """docs/adr/036：主管有所有操作（含開工單、取消工單、排程）；其他帳號沒有排程權限照樣 403。"""
    manager = identity.get_account("manager")
    for op in MANAGER:
        assert manager.can(op), op
    # 主管可以排程（不丟例外）
    security.schedule_check_create(security.SecurityContext(manager, "t", None, "r"))
    for aid in ("guest", "warehouse", "sales", "artist"):
        acc = identity.get_account(aid)
        for op in PRODUCTION:
            assert not acc.can(op), (aid, op)
        with pytest.raises(Exception) as e:
            security.schedule_check_create(security.SecurityContext(acc, "t", None, "r"))
        assert getattr(e.value, "status", None) == 403, aid


def test_background_job_policies_use_the_same_matrix():
    """建立／控制排程只有主管（schedule_run）；3D 重建要工廠圖紙領域＋圖紙可見；
    讀排程要工廠資料庫領域。"""
    accounts = identity.accounts()
    can_schedule = {a for a, acc in accounts.items() if security.schedule_can_control(acc)}
    assert can_schedule == {"manager"}
    can_read_schedule = {a for a, acc in accounts.items() if security.schedule_can_read(acc)}
    assert can_read_schedule == {"warehouse", "sales", "manager"}
    factory = {"warehouse", "manager"}
    for part, expected in (("mfg-002", factory), ("mfg-004", factory | {"sales"})):
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


def test_work_orders_only_manager_and_others_cannot_cancel(client):
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
    assert allowed == {"manager"}
    schedule_service.reset()


def test_region_annotation_is_only_for_assigned_artworks(client):
    """只驗被擋的情況（不寫入）：沒有 kb_annotate 的角色、藝術家對沒分配給他的畫都是 403；
    分配給藝術家的畫能送出草稿的對照組在 test_region_drafts.py。
    主管所有畫都能圈（docs/adr/036），也不在這裡做。"""
    body = {
        "label": "山頂灌木",
        "points": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.3], [0.1, 0.3]],
        "text": "這片山頂的灌木是我小時候第一次看這幅畫時最喜歡的地方，濃墨點得又密又有力。",
        "license": "CC BY 4.0",
        "attribution": "甲",
    }
    for aid in EXPECTED:
        if aid in ("artist", "manager"):
            continue  # 分配給他的畫（主管是全部）會真的建立草稿，不在這裡做
        as_account(client, aid)
        r = client.post("/api/v1/artworks/npm-000001/region-drafts", json=body)
        assert r.status_code == 403, aid
    as_account(client, "artist")
    r = client.post("/api/v1/artworks/aic-27992/region-drafts", json=body)
    assert r.status_code == 403
    as_account(client, DEFAULT_ACCOUNT)
