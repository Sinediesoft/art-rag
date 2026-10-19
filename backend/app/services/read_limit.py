"""機密檔案的讀取頻率限制（內部人員大量下載的防禦）。

同一個帳號在 window_s 秒內下載機密零件的原尺寸圖紙、STEP／STL 模型超過 max_downloads 次就回 429；
超過的那一次記進安全紀錄（誰、哪張、第幾次），主管在安全紀錄頁看得到。主管也一樣算，
內部人員不一定是低權限帳號。縮圖不算：清單一次會載很多張，算進去一般瀏覽就會被擋。

計數放在這個行程的記憶體：後端重啟會歸零；之後開多個 API 行程時要改成 Redis 計數，
否則每個行程各算各的。設定在 shared/access.yaml 的 read_limits。
"""

import threading
import time
from collections import deque

from app.agent import guard
from app.core.config import get_access_config
from app.core.errors import AppError
from app.services.identity import Account

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}


def _cfg() -> dict:
    return get_access_config().get("read_limits") or {}


def reset() -> None:
    with _lock:
        _hits.clear()


def check_secret_download(account: Account, part: dict, what: str, request_id: str) -> None:
    """機密零件的完整檔案下載前呼叫；超過上限就記安全紀錄並丟 429（這次不給檔案）。"""
    cfg = _cfg()
    if part.get("confidentiality") not in cfg.get("levels", ["機密"]):
        return
    window, limit = float(cfg.get("window_s", 600)), int(cfg.get("max_downloads", 6))
    now = time.monotonic()
    with _lock:
        hits = _hits.setdefault(account.id, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        over = len(hits) >= limit
        if not over:
            hits.append(now)
        count = len(hits)
    if over:
        guard.log_block(
            1,
            "機密檔案下載過多",
            account,
            f"{window / 60:.0f} 分鐘內已下載 {count} 次機密檔案，"
            f"這次要 {part['id']} 的 {what}，已拒絕",
            request_id,
            "地端讀取頻率限制",
        )
        raise AppError(
            "RATE_LIMITED",
            f"{window / 60:.0f} 分鐘內下載機密檔案已達 {limit} 次，請稍後再試；"
            "需要大量取用請洽主管。這次的請求已記錄。",
            429,
        )
