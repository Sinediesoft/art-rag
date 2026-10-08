"""智慧助理第 1、2 段 → 問答第 3～7 段的交接票（docs/adr/030）。

/agent/route 放行畫作問答、圖紙問答時，伺服器記下「這個帳號、這句話、這個對象已通過第 1、2 段，
第 4～6 段由誰判斷」，回給前端一張短效的隨機票號；前端呼叫 /chat 時帶著票號。

- 票號只是查表用的隨機字串，內容（帳號、問句雜湊、對象、判斷者）都在伺服器記憶體，前端改不了
- /chat 驗：票號存在、沒過期、帳號相同、問句雜湊相同、對象（圖紙／畫作）相同；任一不符就當沒帶，
  /chat 自己重跑第 2 段——偽造或過期的票只會讓檢查變多，不會變少
- 有效的票也只省掉第 2 段；第 1 段的文件層授權、第 3～7 段 /chat 一律重跑
- 只存在這個後端行程的記憶體（展示版單機）：後端重啟後舊票失效，/chat 會重跑第 2 段
"""

import secrets
import threading
import time
from dataclasses import dataclass

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
    with _lock:
        for k in [k for k, v in _tickets.items() if v.expires <= now]:
            del _tickets[k]
        while len(_tickets) >= MAX_TICKETS:
            del _tickets[next(iter(_tickets))]  # 最舊的先丟
        _tickets[token] = t
    return token


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
