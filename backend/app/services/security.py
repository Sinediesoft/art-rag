"""共用安全上下文與工作／產物授權政策（TASK-20261013-001，docs/adr/036）。

上下文只能由伺服器建立：帳號來自閘道驗過的 JWT 再對回 access.yaml（不看 JWT 裡的角色宣告），
操作與領域由路由程式決定，`request_id` 由中介層產生。請求內容（body、query、header）裡
沒有任何欄位可以指定角色、機密等級、部門或放寬資料範圍。

直接 API、智慧助理入口、背景 worker 與下載端點呼叫同一套政策函式：
- `for_request()`：HTTP 請求（`Depends(secure(...))`）
- `for_account()`：背景 worker 執行前、寫回前依「目前的」access.yaml 重新建立（佇列不存 JWT）

看不到的工作與產物回 404（和不存在同一個回應）；領域或操作權限不足在建立時回 403。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.services import identity, uploads
from app.services.identity import Account

CAD_JOB_NOT_FOUND = ("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果，可能已超過保存期限", 404)
JOB_NOT_FOUND = ("JOB_NOT_FOUND", "找不到這個工作，可能已超過保存期限", 404)


@dataclass(frozen=True)
class SecurityContext:
    """一次操作的安全上下文：誰、做什麼、在哪個領域。資料範圍由 account 推導，不接受外部指定。"""

    account: Account
    operation: str
    domain: str | None
    request_id: str

    # ---- 建立（只有伺服器端能建）
    @classmethod
    def for_request(
        cls,
        request: Request,
        operation: str,
        *,
        domain: str | None = None,
        op: str | None = None,
        what: str | None = None,
    ) -> SecurityContext:
        account = identity.current(request)
        ctx = cls(account, operation, domain, getattr(request.state, "request_id", ""))
        ctx.check(op=op, what=what or operation)
        return ctx

    @classmethod
    def for_account(
        cls,
        account_id: str,
        operation: str,
        *,
        domain: str | None = None,
        op: str | None = None,
        request_id: str = "",
    ) -> SecurityContext:
        """背景 worker 用：依 account_id 對回目前的 access.yaml（撤權、刪帳號立刻生效）。"""
        try:
            account = identity.get_account(account_id)
        except AppError as e:
            raise AppError("PERMISSION_REVOKED", "提交工作的帳號已不存在或已被撤銷", 403) from e
        ctx = cls(account, operation, domain, request_id)
        ctx.check(op=op, what=operation)
        return ctx

    def check(self, *, op: str | None = None, what: str | None = None) -> None:
        if self.domain:
            identity.require_domain(self.account, self.domain)
        if op:
            identity.require(self.account, op, what or self.operation)

    # ---- 資料範圍
    def visible_part_ids(self) -> set[str]:
        return identity.visible_part_ids(self.account, get_store().parts)

    def require_part(self, part_id: str) -> dict:
        p = get_store().get_part(part_id)
        if not p:
            raise AppError("PART_NOT_FOUND", f"找不到圖紙 {part_id}", 404)
        identity.require_part(self.account, p)
        return p


def secure(operation: str, *, domain: str | None = None, op: str | None = None):
    """FastAPI 相依：`ctx = Depends(secure("cad.job.create", domain="mfg"))`。"""

    def dep(request: Request) -> SecurityContext:
        return SecurityContext.for_request(request, operation, domain=domain, op=op)

    return dep


# ----------------------------------------------------------------------------- 3D 重建工作
@dataclass(frozen=True)
class CadAccess:
    """一個 3D 重建工作的存取依據：誰建的、來源資源。工作從建立起就有，不看 summary.json 在不在。"""

    job_id: str
    owner_id: str | None
    part_id: str | None  # 指定的知識庫圖紙
    image_id: str | None  # 來源上傳照片
    identified_part_id: str | None = None  # 照片辨識出的知識庫圖紙
    legacy: bool = False  # 舊資料（沒有存取紀錄）從摘要推導

    @classmethod
    def from_dict(cls, job_id: str, d: dict, *, legacy: bool = False) -> CadAccess:
        return cls(
            job_id=job_id,
            owner_id=d.get("owner_id"),
            part_id=d.get("part_id"),
            image_id=d.get("image_id"),
            identified_part_id=d.get("identified_part_id"),
            legacy=legacy,
        )

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "part_id": self.part_id,
            "image_id": self.image_id,
            "identified_part_id": self.identified_part_id,
        }


def cad_check_create(ctx: SecurityContext, part_id: str | None, image_id: str | None) -> None:
    """建立 3D 重建前的授權（也在 worker 執行前、寫回前重驗）：能用工廠圖紙、指定的圖紙看得到、
    上傳照片是自己的（或擁有者授權的）。任何一項不過都在讀圖、呼叫模型之前擋下。"""
    identity.require_domain(ctx.account, "mfg")
    if part_id:
        ctx.require_part(part_id)
    if image_id:
        uploads.authorize(image_id, ctx.account)


def cad_can_read(account: Account, access: CadAccess) -> bool:
    """讀取工作的狀態、事件、摘要、程式碼、輸入圖、預覽與模型的共同規則。
    - 要能用工廠圖紙（mfg）
    - 來源是知識庫圖紙（指定或辨識出的）：圖紙看得到（機密等級＋部門）
    - 來源是上傳照片：照片屬於這個帳號或擁有者授權給它（擁有者本人當然可以）
    舊資料沒有擁有者：照片來源的一律不能讀（要管理者遷移），只有知識庫圖紙的才依圖紙規則讀。"""
    if not account.can_read("mfg"):
        return False
    store = get_store()
    for pid in (access.part_id, access.identified_part_id):
        if pid:
            p = store.get_part(pid)
            if p is None or not identity.can_view_part(account, p):
                return False
    if access.image_id:
        return uploads.can_use(access.image_id, account) or (
            access.owner_id is not None and access.owner_id == account.id and not access.legacy
        )
    if access.legacy:
        return bool(access.part_id or access.identified_part_id)
    return access.owner_id == account.id or bool(access.part_id or access.identified_part_id)


def cad_require_read(account: Account, access: CadAccess | None) -> CadAccess:
    if access is None or not cad_can_read(account, access):
        raise AppError(*CAD_JOB_NOT_FOUND)
    return access


def cad_can_cancel(account: Account, access: CadAccess) -> bool:
    """取消：建立的人本人（而且仍有讀取權）。"""
    return access.owner_id == account.id and cad_can_read(account, access)


# ----------------------------------------------------------------------------- 排程工作
def schedule_check_create(ctx: SecurityContext) -> None:
    identity.require_domain(ctx.account, "factory")
    identity.require(ctx.account, "schedule_run", "執行排程")


def schedule_can_read(account: Account) -> bool:
    return account.can_read("factory")


def schedule_can_control(account: Account) -> bool:
    """開始、提前停止、取消排程：有 schedule_run 而且能讀工廠資料庫。"""
    return account.can("schedule_run") and account.can_read("factory")


def job_can_read(account: Account, job: dict) -> bool:
    kind = job["kind"]
    if kind == "cad":
        return cad_can_read(
            account, CadAccess.from_dict(job["id"], job["resource"] | {"owner_id": job["owner_id"]})
        )
    if kind == "schedule":
        return schedule_can_read(account)
    return False


def job_can_cancel(account: Account, job: dict) -> bool:
    kind = job["kind"]
    if kind == "cad":
        return cad_can_cancel(
            account, CadAccess.from_dict(job["id"], job["resource"] | {"owner_id": job["owner_id"]})
        )
    if kind == "schedule":
        return schedule_can_control(account)
    return False


def job_require_read(account: Account, job: dict | None) -> dict:
    if job is None or not job_can_read(account, job):
        raise AppError(*JOB_NOT_FOUND)
    return job
