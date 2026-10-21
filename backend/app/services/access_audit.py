"""機密資料的讀取稽核（內部人員演練）：原本稽核只記修改，
伺服器 log 只有「127.0.0.1 下載了哪個檔案」，事後查不出是哪個帳號拿走了機密圖紙。

讀機密零件的原尺寸圖紙、3D 模型、圖紙／庫存／生產明細、以這張圖紙問答時記一筆（帳號、零件、功能），
主管在稽核頁（/audit 的 reads）看得到。只記編號與功能名稱，不存內容。
要記哪些等級：shared/access.yaml 的 read_audit.levels（預設只記機密）。
"""

from app.core.config import get_access_config
from app.core.logging import log
from app.repositories.logs_repo import get_logs_repo
from app.services.identity import Account


def record(account: Account, part: dict | None, what: str, request_id: str) -> None:
    if not part:
        return
    levels = (get_access_config().get("read_audit") or {}).get("levels", ["機密"])
    level = part.get("confidentiality")
    if level not in levels:
        return
    repo = get_logs_repo()
    try:
        repo.add_access_log(
            {
                "created_at": repo.now(),
                "request_id": request_id,
                "account_id": account.id,
                "account_label": account.label,
                "part_id": part["id"],
                "level": level,
                "what": what,
            }
        )
    except Exception as e:  # 稽核寫不進去不擋讀取，但要留下錯誤
        log.error(f"讀取稽核寫入失敗（{part['id']}，{what}）：{type(e).__name__}")
