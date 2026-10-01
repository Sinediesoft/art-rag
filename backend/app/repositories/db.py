"""PostgreSQL + pgvector 連線池（.env 的 DATABASE_URL 有值才用；留空＝檔案索引＋SQLite）。

資料庫跑在 Docker（deploy/docker-compose.yml），見 docs/adr/006。SQL 只寫在 repositories/。
"""

import threading

import psycopg
from pgvector.psycopg import register_vector
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from app.core.config import get_settings


class DatabaseUnavailable(Exception):
    pass


def describe(url: str) -> str:
    """給錯誤訊息用的位址：不含密碼。"""
    d = conninfo_to_dict(url)
    return f"{d.get('host', 'localhost')}:{d.get('port', 5432)}/{d.get('dbname', '')}"


def open_pool(url: str) -> ConnectionPool:
    """先確認連得上並裝好 pgvector（register_vector 要查得到 vector 型別），再開連線池。"""
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.OperationalError as e:
        raise DatabaseUnavailable(
            f"連不上資料庫 {describe(url)}（{str(e).strip()}）。請先啟動資料庫：make db-up"
            "（或 docker compose -f deploy/docker-compose.yml --env-file .env up -d --wait db）；"
            "沒有 Docker 的電腦把 .env 的 DATABASE_URL 留空"
        ) from e
    return ConnectionPool(
        url,
        kwargs={"connect_timeout": 5},
        min_size=1,
        max_size=4,
        open=True,
        configure=register_vector,
        # 資料庫容器重啟後，池裡的舊連線會失效：借出前先確認，壞的換新
        check=ConnectionPool.check_connection,
        timeout=5,
    )


_pool: ConnectionPool | None = None
_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = open_pool(get_settings().database_url)
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None
