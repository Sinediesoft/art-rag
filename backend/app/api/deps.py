"""路由共用的小工具：圖紙存在與可見性檢查、以圖搜圖結果的資料範圍過濾。"""

from fastapi import Request

from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.services import identity


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
