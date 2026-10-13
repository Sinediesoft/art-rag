"""智慧助理第 1、2 段 → 問答第 3～7 段的交接票（docs/adr/030）。

/agent/route 放行畫作問答、圖紙問答時，伺服器記下「這個帳號、這句話、這個對象已通過第 1、2 段，
第 4～6 段由誰判斷」，回給前端一張短效的隨機票號；前端呼叫 /chat 時帶著票號。

- 票號只是查表用的隨機字串，內容（帳號、問句雜湊、對象、判斷者）都在伺服器記憶體，前端改不了
- /chat 驗：票號存在、沒過期、帳號相同、問句雜湊相同、對象（圖紙／畫作）相同；任一不符就當沒帶，
  /chat 自己重跑第 2 段——偽造或過期的票只會讓檢查變多，不會變少
- 有效的票也只省掉第 2 段；第 1 段的文件層授權、第 3～7 段 /chat 一律重跑
- 單機（沒設 REDIS_URL）：只存在這個後端行程的記憶體，後端重啟後舊票失效，/chat 會重跑第 2 段
- 多個 API 行程（有 REDIS_URL）：票存在 Redis（短效 key，TTL 同票的效期），任一行程簽發、
  任一行程核銷；Redis 不通或查不到＝當作沒有票，/chat 重跑第 2 段——多行程不會讓檢查變少
"""

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass

import redis

from app.jobs import coordination

TTL_S = 600  # 10 分鐘：一次問答從路由到串流完成綽綽有餘
MAX_TICKETS = 5000


@dataclass(frozen=True)
class Ticket:
    account_id: str
    question: str  # guard.fingerprint(遮蔽個資後的問句)
    intent: str
    part_id: str | None
    artwork_id: str | None
    engine: str  # 第 4～6 段由誰判斷：jev／local（伺服器決定，不是前端參數）
    guard_engine: str  # 第 2 段實際由誰判斷：jev／local／skip
    request_id: str
    expires: float


_lock = threading.Lock()
_tickets: dict[str, Ticket] = {}


def issue(
    account_id: str,
    question: str,
    intent: str,
    part_id: str | None,
    artwork_id: str | None,
    engine: str,
    guard_engine: str,
    request_id: str,
    now: float | None = None,
) -> str:
    now = now or time.time()
    token = secrets.token_urlsafe(24)
    t = Ticket(
        account_id,
        question,
        intent,
        part_id,
        artwork_id,
        engine,
        guard_engine,
        request_id,
        now + TTL_S,
    )
    if _put_redis(token, t):
        return token
    with _lock:
        for k in [k for k, v in _tickets.items() if v.expires <= now]:
            del _tickets[k]
        while len(_tickets) >= MAX_TICKETS:
            del _tickets[next(iter(_tickets))]  # 最舊的先丟
        _tickets[token] = t
    return token


KEY = "artrag:ticket:"


def _put_redis(token: str, t: Ticket) -> bool:
    r = coordination.client()
    if r is None:
        return False
    try:
        r.set(KEY + token, json.dumps(asdict(t)), ex=TTL_S)
        return True
    except redis.RedisError:
        return False  # Redis 不通：退回行程內（同一個 API 行程仍可核銷；其他行程會重跑第 2 段）


def _get_redis(token: str) -> Ticket | None:
    r = coordination.client()
    if r is None:
        return None
    try:
        raw = r.get(KEY + token)
        return Ticket(**json.loads(raw)) if raw else None
    except (redis.RedisError, ValueError, TypeError):
        return None


def redeem(
    token: str | None,
    account_id: str,
    question: str,
    part_id: str | None,
    artwork_id: str | None,
    now: float | None = None,
) -> Ticket | None:
    """票號有效而且帳號、問句、對象都相符才回 Ticket；否則 None（呼叫端重跑第 2 段）。"""
    if not token:
        return None
    now = now or time.time()
    with _lock:
        t = _tickets.get(token)
    if t is None:
        t = _get_redis(token)
    if t is None or t.expires <= now:
        return None
    if (t.account_id, t.question, t.part_id, t.artwork_id) != (
        account_id,
        question,
        part_id,
        artwork_id,
    ):
        return None
    return t


def clear() -> None:
    with _lock:
        _tickets.clear()
    r = coordination.client()
    if r is not None:
        try:
            for key in r.scan_iter(match=KEY + "*"):
                r.delete(key)
        except redis.RedisError:
            pass
