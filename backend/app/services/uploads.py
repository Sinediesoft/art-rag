"""上傳照片的擁有者與明確授權（TASK-20261013-001，docs/adr/041）。

`image_id` 不是授權憑證：原圖、色彩圖、對位、辨識、問答、建檔、批次與 3D 重建只要用到
上傳的照片，都先過 `authorize()`——只有上傳的帳號（擁有者）與擁有者明確授權的帳號可以用。
舊資料沒有擁有者時沒有人能用（安全拒絕），要用得由管理者遷移（`python -m app.jobs.admin
adopt-uploads`）。找不到和沒有權限回同一個 404，不透露別人的照片存不存在。
"""

from app.core.config import get_settings
from app.core.errors import AppError
from app.repositories.logs_repo import get_logs_repo
from app.services.identity import Account

NOT_FOUND = ("IMAGE_NOT_FOUND", "找不到這張照片，可能已超過保存期限", 404)


def allowed(record: dict, viewer: Account) -> bool:
    owner = record.get("owner_id")
    return bool(owner) and (owner == viewer.id or viewer.id in (record.get("acl") or []))


def record_of(image_id: str, viewer: Account) -> dict:
    """照片的紀錄（含 path）；不存在或不屬於 viewer 一律 404 IMAGE_NOT_FOUND。"""
    rec = get_logs_repo().get_upload_record(image_id)
    if rec is None or not allowed(rec, viewer):
        raise AppError(*NOT_FOUND)
    return rec


def authorize(image_id: str, viewer: Account) -> None:
    record_of(image_id, viewer)


def can_use(image_id: str, viewer: Account) -> bool:
    rec = get_logs_repo().get_upload_record(image_id)
    return rec is not None and allowed(rec, viewer)


def owner_of(image_id: str) -> str | None:
    """照片目前的擁有者（沒有紀錄或舊資料沒有擁有者回 None）。建立衍生資料時記下來源擁有者用。"""
    rec = get_logs_repo().get_upload_record(image_id)
    return rec.get("owner_id") if rec else None


def derived_allowed(image_id: str, source_owner_id: str | None, viewer: Account) -> bool:
    """由這張照片衍生的資料（3D 重建工作的狀態、事件、輸入圖副本、程式碼、預覽、模型）能不能讀：
    跟著照片「目前」的擁有者與授權名單——撤銷分享後衍生資料立刻跟著讀不到，不因為誰建立了
    衍生資料就保留讀取權。照片紀錄超過保存期限被清掉時分享名單已無從確認，只剩建立衍生資料
    當時記下的照片擁有者本人（`source_owner_id`）。"""
    rec = get_logs_repo().get_upload_record(image_id)
    if rec is not None:
        return allowed(rec, viewer)
    return bool(source_owner_id) and source_owner_id == viewer.id


def load(image_id: str, viewer: Account) -> bytes:
    """授權後讀出 JPEG 位元組。這是讀取上傳原檔的唯一入口。"""
    rec = record_of(image_id, viewer)
    return (get_settings().uploads_dir / rec["path"]).read_bytes()


def load_unchecked(image_id: str) -> bytes:
    """只給「呼叫端剛呼叫過 authorize()」的快取函式內部用（結果以 image_id 為鍵快取，
    授權必須在快取之外、每次呼叫都驗）。"""
    rec = get_logs_repo().get_upload_record(image_id)
    if rec is None:
        raise AppError(*NOT_FOUND)
    return (get_settings().uploads_dir / rec["path"]).read_bytes()


def register(image_id: str, filename: str, owner: Account) -> None:
    get_logs_repo().add_upload(image_id, filename, owner.id)


def set_acl(image_id: str, owner: Account, accounts: list[str], known: set[str]) -> list[str]:
    """擁有者設定可以使用這張照片的其他帳號（整份取代）。只有擁有者能改；帳號必須存在。"""
    rec = get_logs_repo().get_upload_record(image_id)
    if rec is None or rec.get("owner_id") != owner.id:
        raise AppError(*NOT_FOUND)
    unknown = [a for a in accounts if a not in known]
    if unknown:
        raise AppError("VALIDATION_ERROR", f"沒有這些帳號：{'、'.join(unknown)}", 422)
    acl = sorted(set(accounts) - {owner.id})
    get_logs_repo().set_upload_acl(image_id, acl)
    return acl
