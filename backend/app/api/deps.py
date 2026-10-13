"""路由共用的小工具：圖紙存在與可見性檢查、以圖搜圖結果的資料範圍過濾。"""

from fastapi import Header, Request

from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.services import identity

# 圖紙、料號這類 id 的結構化驗證（固定查詢的路徑參數）：只收英數字與 . _ -
ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"

IDEMPOTENCY = Header(
    default=None,
    alias="Idempotency-Key",
    description="重送保護：相同帳號、操作與 key 的相同請求只寫入一次，重送拿到先前的回應"
    "（回應帶 Idempotent-Replayed: true）；key 用在不同參數的請求回 409",
)


def part_or_404(part_id: str) -> dict:
    p = get_store().get_part(part_id)
    if not p:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {part_id}", 404)
    return p


def visible_part(part_id: str, request: Request) -> dict:
    """圖紙存在、而且目前身分看得到（identity.can_view_part：領域＋機密等級＋部門）。"""
    p = part_or_404(part_id)
    identity.require_part(identity.current(request), p)
    return p


def scope_drawing_result(result: dict, account: identity.Account) -> dict:
    """以圖搜圖紙的結果只留看得到的圖紙（機密等級＋部門）；照片辨識出的正是看不到的圖紙 → 403。"""
    store = get_store()
    if result["matched"]:
        identity.require_part(account, store.get_part(result["best_part_id"]))
    return {
        **result,
        "results": [
            r
            for r in result["results"]
            if identity.can_view_part(account, store.get_part(r["part"]["id"]))
        ],
    }
