"""工廠資料的 PostgreSQL 連線（docs/adr/048）：FACTORY_DATABASE_URL，留空沿用 DATABASE_URL。

三個 schema：
- production：可寫入的生產資料（工單、排程、異動、核准、稽核、冪等），見 production_repo.py
- inventory：Text-to-SQL 查的庫存資料庫，由知識庫 JSON＋production 重建，見 inventory_repo.py
- factory：系統自己的中繼資料（庫存資料庫的建庫狀態、Text-to-SQL 唯讀角色的密碼）；
  只有主帳號讀得到，唯讀角色連 schema 都看不到

沒有設定資料庫時不退回 SQLite：丟 FactoryUnavailable（503 FACTORY_DB_UNAVAILABLE）。
SQL 只寫在 repositories/。
"""

import hashlib
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.adapt import Loader
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.string import TextLoader
from psycopg_pool import ConnectionPool

from app.core.config import get_settings
from app.core.errors import AppError

NOT_CONFIGURED = (
    "工廠資料（工單、排程、修改資料、庫存查詢）存在 PostgreSQL：請在 .env 設定 DATABASE_URL"
    "（或 FACTORY_DATABASE_URL），並啟動資料庫（docker compose -f deploy/docker-compose.yml"
    " --env-file .env up -d --wait db）"
)


class FactoryUnavailable(AppError):
    """沒有設定工廠資料庫：工廠功能停用（不退回 SQLite）。連得到設定但連不上時是 psycopg 的錯誤
    （由 core/errors.py 轉成 503 DATABASE_UNAVAILABLE）。"""

    def __init__(self, message: str = NOT_CONFIGURED):
        super().__init__("FACTORY_DB_UNAVAILABLE", message, 503)


class _FloatLoader(Loader):
    """numeric 讀成 float：Text-to-SQL 的結果要轉成 JSON，ROUND()／AVG() 的結果是 numeric。"""

    def load(self, data):
        return float(bytes(data).decode())


def configure(conn: psycopg.Connection) -> None:
    """日期時間一律讀成字串（和 SQLite 版相同：欄位本來就是 'YYYY-MM-DD' 文字，
    模型轉型的結果也照字串回傳）。"""
    conn.adapters.register_loader("numeric", _FloatLoader)
    for t in ("date", "time", "timetz", "timestamp", "timestamptz", "interval"):
        conn.adapters.register_loader(t, TextLoader)


def url() -> str:
    u = get_settings().factory_db_url
    if not u:
        raise FactoryUnavailable()
    return u


def configured() -> bool:
    return bool(get_settings().factory_db_url)


def describe(u: str) -> str:
    """給訊息用的位址：不含密碼。"""
    d = conninfo_to_dict(u)
    return f"{d.get('host', 'localhost')}:{d.get('port', 5432)}/{d.get('dbname', '')}"


_pool: ConnectionPool | None = None
_pool_url = ""
_lock = threading.Lock()


def _configure_pool_conn(conn: psycopg.Connection) -> None:
    configure(conn)
    conn.row_factory = dict_row


def get_pool() -> ConnectionPool:
    """主帳號的連線池（dict 列）。設定換了（測試）就換一個新的池。"""
    global _pool, _pool_url
    u = url()
    with _lock:
        if _pool is not None and _pool_url != u:
            _pool.close()
            _pool = None
        if _pool is None:
            _pool = ConnectionPool(
                u,
                kwargs={"connect_timeout": 5},
                min_size=1,
                max_size=8,
                open=True,
                configure=_configure_pool_conn,
                # 資料庫容器重啟後，池裡的舊連線會失效：借出前先確認，壞的換新
                check=ConnectionPool.check_connection,
                timeout=10,
            )
            _pool_url = u
        return _pool


def close_pool() -> None:
    global _pool, _pool_url
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _pool_url = None, ""


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """借一條主帳號連線：區塊正常結束就提交、出例外就回滾（psycopg_pool 的行為）。"""
    with get_pool().connection() as conn:
        yield conn


# 交易層級的 advisory lock：拿到的人一直持有到交易結束（提交或回滾）
def _key(name: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"artrag:{name}".encode()).digest()[:8], "big", signed=True
    )


PRODUCTION_WRITE = _key("production-write")
INVENTORY_BUILD = _key("inventory-build")


def xact_lock(conn: psycopg.Connection, key: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def connect_as(role: str, password: str, options: dict[str, str]) -> psycopg.Connection:
    """用另一個帳號（Text-to-SQL 的唯讀角色）連同一個資料庫；每次查詢一條新連線，用完就關。"""
    opts = " ".join(f"-c {k}={v}" for k, v in options.items())
    conninfo = make_conninfo(url(), user=role, password=password, options=opts, connect_timeout=5)
    conn = psycopg.connect(conninfo, autocommit=False)
    configure(conn)
    return conn


def ident_part(text: str) -> str:
    """資料庫名稱等轉成可以放進角色名稱的 ASCII。"""
    return re.sub(r"[^a-z0-9_]", "_", text.lower())[:20] or "db"
