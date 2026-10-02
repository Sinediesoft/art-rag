"""展示帳號與身分（docs/adr/011）：頁首切換身分，身分只存在伺服器端的工作階段。

- 瀏覽器只拿到隨機的工作階段 cookie（artrag_sid），帳號對應存在後端記憶體；
  後端重啟就回到「訪客」
- 帳號、角色、範圍都在 shared/access.yaml（種子檔，不用密碼）；DEMO_CONTROLS=false 時不能切換
- 任何模型（Jev、Qwen3-VL）都看不到、也決定不了身分：權限只由這裡的帳號決定
- 資料範圍（docs/adr/012，五段防護的第 1 段）：角色能讀哪些領域（畫作／工廠圖紙／工廠資料庫）
  與哪些機密等級；所有讀取 API 用 require_domain()／require_part() 檢查，
  檢索用 levels 產生 Metadata Filter
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
    # 資料範圍：能讀的領域（art／mfg／factory）與看得到的機密等級
    domains: tuple[str, ...] = ("art",)
    levels: tuple[str, ...] = ("公開",)
    scope_note: str = ""

    def can(self, op: str) -> bool:
        return op in self.ops

    def can_read(self, domain: str) -> bool:
        return domain in self.domains

    def can_see(self, level: str) -> bool:
        return level in self.levels

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
            "domains": list(self.domains),
            "levels": list(self.levels),
            "scope_note": self.scope_note,
        }


def accounts() -> dict[str, Account]:
    cfg = get_access_config()
    out = {}
    for aid, a in cfg["accounts"].items():
        role = cfg["roles"][a["role"]]
        # 沒列在 clearance 的角色只能讀公開畫作（預設不允許）
        cl = cfg.get("clearance", {}).get(a["role"], {})
        out[aid] = Account(
            id=aid,
            label=a["label"],
            role=a["role"],
            role_label=role["label"],
            ops=tuple(role.get("ops", [])),
            warehouses=tuple(a.get("warehouses", [])),
            customers=tuple(a.get("customers", [])),
            note=role.get("note", ""),
            domains=tuple(cl.get("domains", ["art"])),
            levels=tuple(cl.get("levels", ["公開"])),
            scope_note=cl.get("note", "只查公開的畫作知識庫"),
        )
    return out


def domain_label(domain: str) -> str:
    return get_access_config().get("domains", {}).get(domain, domain)


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


def who_can_read(domain: str, level: str | None = None, op: str | None = None) -> list[Account]:
    """哪些帳號能讀這個領域（與等級）、能做這個操作：給「切換成〇〇再試一次」與錯誤訊息用。"""
    return [
        a
        for a in accounts().values()
        if a.can_read(domain) and (level is None or a.can_see(level)) and (op is None or a.can(op))
    ]


def _scope_denied(account: Account, what: str, domain: str, level: str | None = None) -> AppError:
    who = "、".join(a.label for a in who_can_read(domain, level)) or "沒有任何身分"
    return AppError(
        "DATA_SCOPE_DENIED",
        f"目前身分「{account.label}」{what}（{account.scope_note}）。可以的身分：{who}。請在頁首切換身分。",
        403,
    )


def require_domain(account: Account, domain: str) -> None:
    """資料範圍：這個身分能不能讀這個領域（工廠圖紙、工廠資料庫）。畫作人人可讀。"""
    if not account.can_read(domain):
        raise _scope_denied(account, f"不能使用「{domain_label(domain)}」", domain)


def require_part(account: Account, part: dict) -> None:
    """資料範圍：工廠圖紙要能讀 mfg 領域，而且看得到這張圖紙的機密等級。"""
    require_domain(account, "mfg")
    level = part.get("confidentiality", "機密")
    if not account.can_see(level):
        raise _scope_denied(account, f"看不到{level}圖紙〈{part['name']['zh']}〉", "mfg", level)
