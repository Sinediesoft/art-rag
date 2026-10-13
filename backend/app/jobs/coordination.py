"""跨行程協調（Redis）：模型使用登記、交接票共用儲存。

每個行程（API、各 worker）把自己「正在使用的模型」以短效 key 登記在 Redis，
memory_guard 釋放模型前除了看本行程的使用中引用，也看其他行程的登記——
不會因為 API 行程判斷記憶體吃緊，就把 worker 正在用的 Ortho2CAD 卸載（反之亦然），
也不會各行程自己判斷而重複載入。Redis 不通時退回只看本行程（記一次警告）。
"""

import json
import logging
import os
import socket
import uuid

import redis

from app.core.config import get_settings

_log = logging.getLogger("artrag.jobs")
PREFIX = "artrag:models:use:"
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
_warned = False
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
        _log.warning("Redis 不可用，模型使用登記只看本行程：%s", e)


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


def others_in_use() -> set[str]:
    """其他行程登記的使用中模型。"""
    r = client()
    if r is None:
        return set()
    out: set[str] = set()
    try:
        for key in r.scan_iter(match=PREFIX + "*", count=100):
            if key == PREFIX + HOLDER:
                continue
            raw = r.get(key)
            if raw:
                out |= set(json.loads(raw))
    except (redis.RedisError, ValueError) as e:
        _warn(e)
    return out
