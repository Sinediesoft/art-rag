"""展示帳號與身分：頁首切換身分，後端簽發 JWT（docs/adr/011、015）。

- 七段權限控管的第 1 段「認證與授權」：切換身分時後端簽發 JWT（HS256），內容是 roles、
  部門（dept／depts）、機密等級（clearance）與效期；瀏覽器把它存在 HttpOnly cookie（artrag_token），
  API 也接受 Authorization: Bearer。所有 /api/v1 請求先過 gateway()：沒帶、簽章不符、過期 → 401，
  請求碰不到任何模型與資料
- 帳號、角色、範圍都在 shared/access.yaml（種子檔，不用密碼）。切換身分只在展示模式開放：
  DEMO_CONTROLS=true（預設 false，人在本機 .env 明確開啟）＋請求來自 DEMO_TRUSTED_HOSTS
  ＋已經有有效憑證；正式環境不能自己簽發任何帳號的 JWT（docs/adr/030）
- 任何模型（Jev、Qwen3-VL）都看不到、也決定不了身分：權限只由憑證裡的帳號與角色決定
- 資料範圍：角色能讀哪些領域（畫作／工廠圖紙／工廠資料庫）、哪些機密等級與部門；
  圖紙看不看得到只由 can_view_part() 決定（領域＋機密等級＋部門，docs/adr/030），
  單筆、清單、搜尋、比較、3D、問答與生成前置檢查都用它；
  檢索用同一條規則產生 Metadata Filter（第 3 段）
- 管理與診斷畫面（診斷、拒絕並記錄、稽核）照 access.yaml 的 views 檢查角色（require_view）
"""

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import Request, Response

from app.core import jwt
from app.core.config import get_access_config, get_settings
from app.core.errors import AppError

COOKIE = "artrag_token"
ISSUER = "art-rag"
TW = timezone(timedelta(hours=8))
# 不用憑證的端點：健康檢查（只回存活與就緒）、取得憑證（/auth/accounts 沒有有效憑證時發訪客憑證）。
# 切換身分不在這裡：沒有憑證就在閘道 401（docs/adr/030）
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/auth/accounts"}


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
    # 部門：自己的部門、讀得到哪些部門的文件（Metadata Filter 的 dept 條件）
    dept: str = ""
    depts: tuple[str, ...] = ()
    # 看得到哪些管理與診斷畫面（access.yaml 的 views）
    views: tuple[str, ...] = ()

    @property
    def clearance(self) -> int:
        """機密等級：levels 的最高一級（公開 0、內部 1、機密 2）。"""
        return max((level_rank(x) for x in self.levels), default=0)

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
            "dept": self.dept,
            "depts": list(self.depts),
            "clearance": self.clearance,
            "views": list(self.views),
        }


def accounts() -> dict[str, Account]:
    cfg = get_access_config()
    out = {}
    for aid, a in cfg["accounts"].items():
        role = cfg["roles"][a["role"]]
        # 沒列在 clearance 的角色只能讀公開畫作（預設不允許）
        cl = cfg.get("clearance", {}).get(a["role"], {})
        views = tuple(v for v, roles in cfg.get("views", {}).items() if a["role"] in roles)
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
            dept=cl.get("dept", ""),
            depts=tuple(cl.get("depts", [])),
            views=views,
        )
    return out


def level_rank(level: str) -> int:
    """機密等級的順序：公開 0、內部 1、機密 2（shared/access.yaml 的 levels）。"""
    order = get_access_config().get("levels", ["公開", "內部", "機密"])
    return order.index(level) if level in order else len(order)


def scope_allows(clearance: int, depts, level: str, dept: str) -> bool:
    """機密等級與部門的共同規則：等級不高於 clearance，而且部門是「公開」或在 depts 裡。
    第 3 段的 Metadata Filter（guard.MetaFilter）與 can_view_part() 都用這一條，
    不會一處放行、一處擋。
    沒有部門的文件（dept 空白）一律看不到（預設不允許）。"""
    return level_rank(level) <= clearance and (dept == "公開" or (bool(dept) and dept in depts))


def domain_label(domain: str) -> str:
    return get_access_config().get("domains", {}).get(domain, domain)


def get_account(account_id: str) -> Account:
    acc = accounts().get(account_id)
    if not acc:
        raise AppError("ACCOUNT_NOT_FOUND", f"沒有這個展示帳號：{account_id}", 404)
    return acc


@dataclass
class Auth:
    """第 1 段驗證過的憑證：帳號（權限照 access.yaml 的角色）與 JWT 內容。"""

    account: Account
    claims: dict
    token: str
    via: str  # cookie／header
    checks: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        exp = datetime.fromtimestamp(self.claims["exp"], TW)
        return {
            "claims": self.claims,
            "expires_at": exp.isoformat(timespec="seconds"),
            "via": self.via,
            "alg": jwt.ALG,
            # 只給前端看 header.payload（示範竄改憑證用）；簽章不外流到畫面
            "unsigned": ".".join(self.token.split(".")[:2]),
            "checks": self.checks,
        }


@lru_cache
def _secret() -> str:
    # 留空＝每次啟動隨機產生：後端重啟後舊憑證全部失效（前端自動改回訪客）
    return get_settings().jwt_secret or secrets.token_urlsafe(32)


def issue(acc: Account, now: float | None = None) -> str:
    now = int(now or time.time())
    claims = {
        "iss": ISSUER,
        "sub": acc.id,
        "name": acc.label,
        "roles": [acc.role_label],
        "dept": acc.dept,
        "depts": list(acc.depts),
        "clearance": acc.clearance,
        "iat": now,
        "exp": now + get_settings().jwt_ttl_min * 60,
    }
    return jwt.encode(claims, _secret())


def token_of(request: Request) -> tuple[str, str] | None:
    """憑證：Authorization: Bearer 優先，其次 HttpOnly cookie。"""
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip(), "header"
    c = request.cookies.get(COOKIE)
    return (c, "cookie") if c else None


def verify(request: Request) -> Auth:
    """第 1 段「認證」：驗簽章、效期、簽發者，再對回 access.yaml 的帳號。
    失敗 raise jwt.TokenError。"""
    got = token_of(request)
    if not got:
        raise jwt.TokenError("UNAUTHENTICATED", "沒有身分憑證（JWT），請先取得憑證")
    token, via = got
    claims = jwt.decode(token, _secret(), ISSUER)
    acc = accounts().get(str(claims.get("sub")))
    if not acc:
        raise jwt.TokenError("TOKEN_INVALID", f"憑證裡的帳號不存在：{claims.get('sub')}")
    left = int(claims["exp"] - time.time())
    exp = datetime.fromtimestamp(claims["exp"], TW).strftime("%H:%M")
    checks = [
        {
            "key": "signature",
            "label": "簽章",
            "ok": True,
            "detail": f"{jwt.ALG} 簽章相符，由本系統簽發",
        },
        {
            "key": "expiry",
            "label": "效期",
            "ok": True,
            "detail": f"到 {exp}（還有 {left // 60} 分鐘）",
        },
        {
            "key": "claims",
            "label": "角色",
            "ok": True,
            "detail": f"roles={json.dumps(claims['roles'], ensure_ascii=False)}"
            f"・dept={claims['dept']}"
            f"・clearance={claims['clearance']}",
        },
    ]
    return Auth(acc, claims, token, via, checks)


def gateway(request: Request) -> tuple[str, str] | None:
    """API 閘道：/api/v1 的請求先驗憑證，通過就把 Auth 放進 request.state.auth。
    回傳 (錯誤碼, 訊息) 表示要直接回 401；None＝放行。不用憑證的端點也會試著解出身分。"""
    path = request.url.path
    if not path.startswith("/api/v1/"):
        return None
    try:
        request.state.auth = verify(request)
    except jwt.TokenError as e:
        request.state.auth = None
        if path in PUBLIC_PATHS:
            request.state.auth_error = e
            return None
        return e.code, e.message
    return None


def auth_of(request: Request) -> Auth:
    auth = getattr(request.state, "auth", None)
    if auth is None:  # 閘道已擋；直接呼叫函式的測試才會走到這裡
        raise AppError("UNAUTHENTICATED", "沒有身分憑證（JWT），請先取得憑證", 401)
    return auth


def current(request: Request) -> Account:
    """目前身分：閘道驗證過的 JWT 對應的帳號。"""
    return auth_of(request).account


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=get_settings().jwt_ttl_min * 60,
    )


def ensure(request: Request, response: Response) -> Auth:
    """/auth/accounts：有有效憑證就用；沒有或失效就發一張預設帳號（訪客）的憑證。"""
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return auth
    acc = get_account(get_access_config()["default_account"])
    token = issue(acc)
    _set_cookie(response, token)
    err = getattr(request.state, "auth_error", None)
    a = Auth(acc, jwt.peek(token), token, "cookie")
    a.checks = [
        {
            "key": "issued",
            "label": "簽發",
            "ok": True,
            "detail": f"{err.message if err else '還沒有憑證'}，已改發〈{acc.label}〉的憑證",
        }
    ]
    return a


def client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def demo_allowed(request: Request) -> bool:
    """展示模式：人在 .env 開了 DEMO_CONTROLS，而且請求來自信任的主機（預設只有本機 loopback）。"""
    s = get_settings()
    return s.demo_controls and client_host(request) in s.demo_trusted_hosts


def require_demo(request: Request, what: str) -> None:
    """展示用功能（切換身分、模擬斷線、展示還原、手動釋放模型）的共同檢查（docs/adr/030）。"""
    s = get_settings()
    if not s.demo_controls:
        raise AppError("FORBIDDEN", f"展示模式已停用（DEMO_CONTROLS=false），不能{what}", 403)
    if client_host(request) not in s.demo_trusted_hosts:
        raise AppError("FORBIDDEN", f"展示模式只接受本機的請求，不能從這台主機{what}", 403)


def eval_allowed(request: Request) -> bool:
    """評估模式（EVAL_CONTROLS）：engine=local、關檢索、strategy=mock 這類對照組選項才會生效。"""
    s = get_settings()
    return s.eval_controls and client_host(request) in s.demo_trusted_hosts


def switch(request: Request, response: Response, account_id: str) -> Auth:
    """展示版切換身分：不用密碼簽發任何帳號的 JWT，所以只在展示模式、本機、
    而且已經有有效憑證（閘道驗過）時開放；其他情況 401／403，不會簽發。"""
    require_demo(request, "切換身分")
    auth_of(request)  # 閘道已擋沒有憑證的請求；直接呼叫函式時也一樣要有
    acc = get_account(account_id)
    token = issue(acc)
    _set_cookie(response, token)
    return Auth(acc, jwt.peek(token), token, "cookie")


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


def can_view_part(account: Account, part: dict) -> bool:
    """單一圖紙可見性（docs/adr/030）：能讀 mfg 領域、看得到這個機密等級、部門在範圍內，三者都要。
    單筆讀取、清單、以圖搜圖紙、文字搜圖紙、比較、3D、批次辨識、問答與生成前置檢查都用這一個函式。"""
    level = part.get("confidentiality", "機密")
    return (
        account.can_read("mfg")
        and account.can_see(level)
        and scope_allows(account.clearance, account.depts, level, part.get("owner", ""))
    )


def visible_part_ids(account: Account, parts) -> set[str]:
    return {p["id"] for p in parts if can_view_part(account, p)}


def require_part(account: Account, part: dict) -> None:
    """資料範圍：看不到的圖紙 → 403。訊息不寫圖紙名稱與等級（不透露是哪一份文件）。"""
    require_domain(account, "mfg")
    if not can_view_part(account, part):
        raise AppError(
            "DATA_SCOPE_DENIED",
            f"目前身分「{account.label}」看不到這張圖紙（{account.scope_note}）。",
            403,
        )


def require_view(account: Account, view: str, what: str) -> None:
    """管理與診斷畫面（access.yaml 的 views）：沒列到的角色一律 403，只有 JWT 不夠。"""
    if view in account.views:
        return
    cfg = get_access_config()
    roles = cfg.get("views", {}).get(view, [])
    labels = "、".join(cfg["roles"][r]["label"] for r in roles) or "沒有任何身分"
    raise AppError(
        "PERMISSION_DENIED", f"目前身分「{account.label}」不能查看{what}（只有{labels}可以）。", 403
    )
