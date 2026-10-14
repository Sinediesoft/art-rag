"""跨行程協調（Redis）：模型使用登記、交接票共用儲存。

每個行程（API、各 worker）把自己「正在使用的模型」以短效 key 登記在 Redis（維護執行緒與
工作心跳定期續約），memory_guard 釋放模型前除了看本行程的使用中引用，也看其他行程的登記——
不會因為 API 行程判斷記憶體吃緊，就把 worker 正在用的 Ortho2CAD 卸載（反之亦然），
也不會各行程自己判斷而重複載入。

查詢結果分「確認」與「未知」：Redis 不通、回傳壞資料，或 Redis 剛重啟（其他行程的登記隨重啟
消失、還沒全部續約回來）時回 None＝未知，memory_guard 保守地不卸載共用模型並回報降級狀態。
"""

import json
import logging
import os
import socket
import time
import uuid

import redis

from app.core.config import get_settings

_log = logging.getLogger("artrag.jobs")
PREFIX = "artrag:models:use:"
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_warned = False
_state: dict = {"state": None, "detail": None}  # 最近一次查詢其他行程使用狀況的結果
_client: redis.Redis | None = None
_client_url: str | None = None


def client() -> redis.Redis | None:
    """共用的 Redis 連線（短逾時）；沒有設定 REDIS_URL 回 None。"""
    global _client, _client_url
    url = get_settings().redis_url
    if not url:
        return None
    if _client is None or _client_url != url:
        _client = redis.Redis.from_url(
            url, socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True
        )
        _client_url = url
    return _client


def _warn(e: Exception) -> None:
    global _warned
    if not _warned:
        _warned = True
        _log.warning("Redis 不可用：%s", e)


WORKER_PREFIX = "artrag:worker:"
WORKER_TTL_S = 15


def register_worker(name: str, queues: list[str]) -> None:
    """worker 行程的存活登記（短效 key，維護執行緒定期續約）：健康狀態頁看得到哪些 worker 活著。"""
    r = client()
    if r is None:
        return
    try:
        body = {"name": name, "queues": queues, "pid": os.getpid(), "at": time.time()}
        r.set(WORKER_PREFIX + name, json.dumps(body), ex=WORKER_TTL_S)
    except redis.RedisError as e:
        _warn(e)


def workers() -> list[dict]:
    r = client()
    if r is None:
        return []
    out = []
    try:
        for key in r.scan_iter(match=WORKER_PREFIX + "*", count=100):
            raw = r.get(key)
            if raw:
                w = json.loads(raw)
                out.append({**w, "age_s": round(max(0.0, time.time() - w["at"]), 1)})
    except (redis.RedisError, ValueError) as e:
        _warn(e)
    return sorted(out, key=lambda w: w["name"])


def publish_usage(in_use: dict[str, int]) -> None:
    """登記本行程目前使用中的模型（TTL 內沒續約就自動消失，行程當掉不會永遠占住）。"""
    r = client()
    if r is None:
        return
    keys = sorted(k for k, n in in_use.items() if n > 0)
    try:
        if keys:
            r.set(PREFIX + HOLDER, json.dumps(keys), ex=int(get_settings().jobs_models_ttl_s))
        else:
            r.delete(PREFIX + HOLDER)
    except redis.RedisError as e:
        _warn(e)


def restart_grace_s() -> float:
    """Redis 重啟後多久之內登記可能不完整：每個還活著的行程都會在登記存活時間內續約，
    所以預設等滿一個登記存活時間（120 秒）才相信「沒有登記＝沒人在用」。"""
    return get_settings().jobs_models_restart_grace_s


def _set_state(state: str, detail: str | None = None) -> None:
    """記下最近一次查詢的協調狀態（給記憶體狀態頁與釋放紀錄）；狀態改變時寫一筆日誌。"""
    prev = _state["state"]
    _state.update(state=state, detail=detail)
    if state != prev:
        if state == "ok":
            if prev is not None:
                _log.info("模型使用登記恢復正常")
        else:
            _log.warning("無法確認其他行程的模型使用狀況（%s），共用模型不卸載：%s", state, detail)


def status() -> dict:
    """最近一次查詢的協調狀態：mode＝local（沒有 REDIS_URL，單一行程）／redis；
    state＝ok／unavailable（Redis 不通）／warming（Redis 剛重啟，等其他行程重新登記）。"""
    if client() is None:
        return {"mode": "local", "state": "ok", "detail": None}
    if _state["state"] is None:
        others_in_use()
    return {"mode": "redis", **_state}


def others_in_use() -> set[str] | None:
    """其他行程登記的使用中模型。

    回 None＝使用情況「未知」，呼叫端要當成可能有人在用：
    - Redis 連不上或回傳壞資料
    - Redis 啟動不到 restart_grace_s()：重啟會清掉所有登記，其他行程還沒全部續約回來
    沒有設定 REDIS_URL（單一行程，沒有背景 worker）回空集合：確認沒有其他行程。"""
    r = client()
    if r is None:
        return set()
    out: set[str] = set()
    try:
        uptime = float(r.info("server").get("uptime_in_seconds", 0))
        grace = restart_grace_s()
        if uptime < grace:
            _set_state("warming", f"Redis 啟動 {uptime:.0f} 秒，未滿 {grace:.0f} 秒")
            return None
        for key in r.scan_iter(match=PREFIX + "*", count=100):
            if key == PREFIX + HOLDER:
                continue
            raw = r.get(key)
            if raw:
                out |= set(json.loads(raw))
    except (redis.RedisError, ValueError, TypeError) as e:
        _warn(e)
        _set_state("unavailable", type(e).__name__)
        return None
    _set_state("ok")
    return out
