"""寫入的 Idempotency-Key（TASK-20261013-001，docs/adr/036）。

相同帳號、操作與 key 的相同請求只會寫入一次：重送（網路中斷、使用者連按、前端重試）拿到先前的回應，
不會重複開工單、重複異動或重複核准；key 用在參數不同的請求回 409 IDEMPOTENCY_KEY_REUSED；
同一個請求還在執行回 409 IDEMPOTENCY_IN_PROGRESS。記錄存在生產資料庫（SQLite，跨 API 行程共用），
和寫入同一個檔案。沒有帶 key 的請求行為不變。
"""

import hashlib
import json
import re
from collections.abc import Callable

from app.core.errors import AppError
from app.repositories.production_repo import get_production_repo

KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def run(account_id: str, operation: str, key: str | None, payload: object, fn: Callable[[], dict]):
    """回傳 (回應, 是否重送)。fn 丟 AppError（沒有寫入）時放掉 key，讓修正後的請求可以重送。"""
    if key is None:
        return fn(), False
    if not KEY.match(key):
        raise AppError(
            "VALIDATION_ERROR", "Idempotency-Key 要 8～128 個英數字元或 . _ : -（建議用 UUID）", 422
        )
    repo = get_production_repo()
    rhash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    state, prev = repo.idem_begin(account_id, operation, key, rhash)
    if state == "replay":
        return prev, True
    if state == "conflict":
        raise AppError(
            "IDEMPOTENCY_KEY_REUSED",
            "這個 Idempotency-Key 已經用在參數不同的請求上，請換一個 key 或使用相同的參數",
            409,
        )
    if state == "busy":
        raise AppError("IDEMPOTENCY_IN_PROGRESS", "相同的請求還在處理中，請稍後再查結果", 409)
    try:
        out = fn()
    except BaseException:
        repo.idem_abort(account_id, operation, key)
        raise
    repo.idem_finish(account_id, operation, key, out)
    return out, False
