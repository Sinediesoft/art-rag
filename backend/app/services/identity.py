"""展示帳號與身分（docs/adr/007）：頁首切換身分，身分只存在伺服器端的工作階段。

- 瀏覽器只拿到隨機的工作階段 cookie（artrag_sid），帳號對應存在後端記憶體；
  後端重啟就回到「訪客」
- 帳號、角色、範圍都在 shared/access.yaml（種子檔，不用密碼）；DEMO_CONTROLS=false 時不能切換
- 任何模型（Jev、Qwen3-VL）都看不到、也決定不了身分：權限只由這裡的帳號決定
"""

import secrets
import threading
from dataclasses import dataclass

from fastapi import Request, Response

from app.core.config import get_access_config, get_settings
from app.core.errors import AppError

COOKIE = "artrag_sid"


@dataclass(frozen=True)
class Account:
    id: str
    label: str
    role: str
    role_label: str
    ops: tuple[str, ...]
    warehouses: tuple[str, ...] = ()
    customers: tuple[str, ...] = ()
    note: str = ""

    def can(self, op: str) -> bool:
        return op in self.ops

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "role": self.role,
            "role_label": self.role_label,
            "ops": list(self.ops),
            "warehouses": list(self.warehouses),
            "customers": list(self.customers),
            "note": self.note,
        }


def accounts() -> dict[str, Account]:
    cfg = get_access_config()
    out = {}
    for aid, a in cfg["accounts"].items():
        role = cfg["roles"][a["role"]]
        out[aid] = Account(
            id=aid,
            label=a["label"],
            role=a["role"],
            role_label=role["label"],
            ops=tuple(role.get("ops", [])),
            warehouses=tuple(a.get("warehouses", [])),
            customers=tuple(a.get("customers", [])),
            note=role.get("note", ""),
        )
    return out


def get_account(account_id: str) -> Account:
    acc = accounts().get(account_id)
    if not acc:
        raise AppError("ACCOUNT_NOT_FOUND", f"沒有這個展示帳號：{account_id}", 404)
    return acc


_sessions: dict[str, str] = {}
_lock = threading.Lock()


def current(request: Request) -> Account:
    """目前身分：沒有工作階段或對不到就是預設帳號（訪客）。"""
    sid = request.cookies.get(COOKIE, "")
    with _lock:
        account_id = _sessions.get(sid)
    return get_account(account_id or get_access_config()["default_account"])


def switch(request: Request, response: Response, account_id: str) -> Account:
    if not get_settings().demo_controls:
        raise AppError("FORBIDDEN", "展示控制已停用，不能切換身分", 403)
    acc = get_account(account_id)
    sid = request.cookies.get(COOKIE) or ""
    with _lock:
        if sid not in _sessions:
            sid = secrets.token_urlsafe(18)
        _sessions[sid] = acc.id
    response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return acc


def require(account: Account, op: str, what: str) -> None:
    """API 層的權限檢查（圖紙頁開立工單、取消工單、開始排程、展示還原都走這裡）。"""
    if account.can(op):
        return
    who = "、".join(a.label for a in accounts().values() if a.can(op)) or "沒有任何身分"
    raise AppError(
        "PERMISSION_DENIED",
        f"目前身分「{account.label}」沒有{what}的權限（可以的身分：{who}）。請在頁首切換身分。",
        403,
    )
