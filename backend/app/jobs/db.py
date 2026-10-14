"""工作資料庫連線池（PostgreSQL，不需要 pgvector）。連不上丟 JobsUnavailable，路由轉成 503。"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.core.config import get_settings


class JobsUnavailable(Exception):
    """Redis 或工作資料庫無法使用：回 503 JOBS_UNAVAILABLE（可恢復，不退回 API 行程執行）。"""

    def __init__(self, message: str, what: str = "database"):
        super().__init__(message)
        self.message = message
        self.what = what


def describe(url: str) -> str:
    d = conninfo_to_dict(url)
    return f"{d.get('host', 'localhost')}:{d.get('port', 5432)}/{d.get('dbname', '')}"


_pool: ConnectionPool | None = None
_lock = threading.Lock()
_url: str | None = None
_down_until = 0.0  # 剛失敗過：這個時間之前直接回 JobsUnavailable，不讓每個請求都等逾時
FAIL_FAST_S = 1.0


def get_pool() -> ConnectionPool:
    global _pool, _url, _down_until
    url = get_settings().jobs_db_url
    if not url:
        raise JobsUnavailable("沒有設定工作資料庫（JOBS_DATABASE_URL 或 DATABASE_URL）")
    with _lock:
        if _pool is None and time.monotonic() < _down_until:
            raise JobsUnavailable(f"連不上工作資料庫 {describe(url)}")
        if _pool is not None and _url != url:  # 測試或設定切換：換成新的資料庫
            _pool.close()
            _pool = None
        if _pool is None:
            try:
                _pool = ConnectionPool(
                    url,
                    kwargs={"connect_timeout": 5, "row_factory": dict_row},
                    min_size=1,
                    max_size=12,
                    open=False,
                    check=ConnectionPool.check_connection,
                    timeout=5,
                )
                _pool.open(wait=True, timeout=6)
                _url = url
            except (psycopg.Error, PoolTimeout) as e:
                if _pool is not None:
                    _pool.close()
                _pool = None
                _down_until = time.monotonic() + FAIL_FAST_S
                raise JobsUnavailable(f"連不上工作資料庫 {describe(url)}") from e
        return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """一個交易：離開時 commit、例外時 rollback。資料庫的錯誤統一轉成 JobsUnavailable。"""
    pool = get_pool()
    try:
        with pool.connection() as conn:
            yield conn
    except (psycopg.OperationalError, PoolTimeout) as e:
        if _connection_level(e):
            _discard(pool)  # 資料庫重啟後舊連線池會一直重試退讓，丟掉它，下一次呼叫建新的
        raise JobsUnavailable(f"工作資料庫暫時無法使用：{type(e).__name__}") from e


def _connection_level(e: Exception) -> bool:
    """連線層的失敗（連不上、連線中斷、伺服器關閉）才重建連線池；死結、逾時這類語句層錯誤不用。"""
    if isinstance(e, PoolTimeout):
        return True
    state = getattr(e, "sqlstate", None)
    return state is None or state.startswith(("08", "57"))


def _discard(pool: ConnectionPool) -> None:
    global _pool, _down_until
    with _lock:
        if _pool is pool:
            _pool = None
            _down_until = time.monotonic() + FAIL_FAST_S
    pool.close(timeout=1)


def close() -> None:
    global _pool, _url, _down_until
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _url, _down_until = None, None, 0.0


def ping() -> bool:
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return True
    except JobsUnavailable:
        return False
