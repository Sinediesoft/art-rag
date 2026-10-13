"""背景工作的稽核紀錄（寫進生產資料庫的 audit_log，主管在 /audit 看得到）。

只記必要的識別碼與結果：誰、做了什麼、哪個工作或排程結果、request_id。不記參數內容、不記模型輸出。
稽核寫入失敗不能讓工作失敗（記一筆日誌就好）。
"""

import logging

from app.repositories.production_repo import get_production_repo
from app.services.identity import Account

_log = logging.getLogger("artrag.jobs")


def record(
    actor: Account | None,
    action: str,
    op: str,
    ref_no: str,
    summary: str,
    request_id: str | None = None,
    detail: dict | None = None,
    actor_id: str | None = None,
) -> None:
    try:
        get_production_repo().add_audit(
            {
                "actor_id": actor.id if actor else actor_id or "system",
                "actor_label": actor.label if actor else actor_id or "系統",
                "action": action,
                "op": op,
                "ref_no": ref_no,
                "summary": summary,
                "detail": detail,
                "request_id": request_id,
            }
        )
    except Exception as e:  # noqa: BLE001 — 稽核失敗不影響工作
        _log.warning("audit failed: %s", e)
