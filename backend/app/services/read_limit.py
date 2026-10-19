"""機密檔案的讀取頻率限制（內部人員大量下載的防禦）。

同一個帳號在 window_s 秒內下載機密零件的原尺寸圖紙、STEP／STL 模型超過 max_downloads 次就回 429；
超過的那一次記進安全紀錄（誰、哪張、第幾次），主管在安全紀錄頁看得到。主管也一樣算，
內部人員不一定是低權限帳號。縮圖不算：清單一次會載很多張，算進去一般瀏覽就會被擋。

有 REDIS_URL 時計數放在 Redis（各 API 行程共用，後端重啟也不歸零）；沒有或 Redis 不通時放在
這個行程的記憶體——退回時限制照樣有，只是各行程各算各的。設定在 shared/access.yaml 的 read_limits。
"""

import threading
import time
import uuid
from collections import deque

import redis

from app.agent import guard
from app.core.config import get_access_config
from app.core.errors import AppError
from app.jobs import coordination
from app.services.identity import Account

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}
KEY = "artrag:readlimit:"
# 滑動時間窗：清掉過期的、沒到上限才加一筆，三步要一起做（多個行程同時下載時不會多放）
_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[2])
local n = redis.call('ZCARD', KEYS[1])
if n >= tonumber(ARGV[3]) then return {1, n} end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])))
return {0, n + 1}
"""


def _cfg() -> dict:
    return get_access_config().get("read_limits") or {}


def reset() -> None:
    with _lock:
        _hits.clear()
    r = coordination.client()
    if r is not None:
        try:
            for key in r.scan_iter(match=KEY + "*"):
                r.delete(key)
        except redis.RedisError:
            pass


def _count_redis(account_id: str, window: float, limit: int) -> tuple[bool, int] | None:
    r = coordination.client()
    if r is None:
        return None
    try:
        over, n = r.eval(_LUA, 1, KEY + account_id, time.time(), window, limit, uuid.uuid4().hex)
        return bool(over), int(n)
    except redis.RedisError:
        return None  # Redis 不通：改在行程內計數


def _count_local(account_id: str, window: float, limit: int) -> tuple[bool, int]:
    now = time.monotonic()
    with _lock:
        hits = _hits.setdefault(account_id, deque())
        while hits and now - hits[0] > window:
            hits.popleft()
        over = len(hits) >= limit
        if not over:
            hits.append(now)
        return over, len(hits)


def check_secret_download(account: Account, part: dict, what: str, request_id: str) -> None:
    """機密零件的完整檔案下載前呼叫；超過上限就記安全紀錄並丟 429（這次不給檔案）。"""
    cfg = _cfg()
    if part.get("confidentiality") not in cfg.get("levels", ["機密"]):
        return
    window, limit = float(cfg.get("window_s", 600)), int(cfg.get("max_downloads", 6))
    over, count = _count_redis(account.id, window, limit) or _count_local(account.id, window, limit)
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
