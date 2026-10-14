"""寫入的 Idempotency-Key（TASK-20261013-001，docs/adr/041）。

相同帳號、操作與 key 的相同請求只會寫入一次：重送（網路中斷、使用者連按、前端重試）拿到先前的回應，
不會重複開工單、重複異動或重複核准；key 用在參數不同的請求回 409 IDEMPOTENCY_KEY_REUSED；
同一個請求還在執行回 409 IDEMPOTENCY_IN_PROGRESS。記錄存在生產資料庫（SQLite，跨 API 行程共用），
和寫入同一個檔案。沒有帶 key 的請求行為不變。

行程在「業務寫入已提交」與「記錄回應」之間中斷也不會寫第二次（TASK-20261013-001 審查 F1）：
寫入點（production_repo 的開／取消工單、寫入異動、送核准）在自己的交易裡把記錄標成 applied，
業務寫入與標記同生同滅。之後同一個 key 重送，狀態是 applied 就不再執行 fn，改呼叫 resume(標記)
補做收尾（工廠資料庫重建、讀回、稽核——都是可重做、不會重複寫入的步驟）並回覆原本的結果。
"""

import hashlib
import json
import re
from collections.abc import Callable

from app.core.errors import AppError
from app.repositories.production_repo import IdempotencyLost, get_production_repo, idem_scope

KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def run(
    account_id: str,
    operation: str,
    key: str | None,
    payload: object,
    fn: Callable[[], dict],
    resume: Callable[[dict], dict] | None = None,
):
    """回傳 (回應, 是否重送)。fn 丟 AppError（沒有寫入）時放掉 key，讓修正後的請求可以重送；
    fn 在業務寫入提交之後才出錯（或行程中斷）時 key 留著，重送由 resume 補做收尾。
    resume(標記)：標記有 ref（單號）與寫入點補充的資料；只能做可重做的收尾，不能再寫入。"""
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
    if state == "busy" or (state == "resume" and resume is None):
        raise AppError("IDEMPOTENCY_IN_PROGRESS", "相同的請求還在處理中，請稍後再查結果", 409)
    if state == "resume":  # 業務寫入已經提交過：只補收尾，絕不再執行 fn
        try:
            out = resume(prev)  # type: ignore[misc]
        except BaseException:
            repo.idem_abort(account_id, operation, key)
            raise
        repo.idem_finish(account_id, operation, key, out)
        return out, True
    try:
        with idem_scope(account_id, operation, key):
            out = fn()
    except IdempotencyLost:  # 這個 key 已經被別的請求接手：這次的業務寫入沒有提交
        raise AppError(
            "IDEMPOTENCY_IN_PROGRESS", "相同的請求已由另一個處理程序接手，請稍後再查結果", 409
        ) from None
    except BaseException:
        repo.idem_abort(account_id, operation, key)
        raise
    repo.idem_finish(account_id, operation, key, out)
    return out, False
