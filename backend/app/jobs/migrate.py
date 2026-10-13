"""版本化 migration：`migrations/NNNN_name.up.sql` 依序套用，`schema_migrations` 記版本與雜湊。

- `apply()`：套用還沒套用的；已套用的檔案被改過（雜湊不同）就拒絕，不靜默覆蓋
- `rollback(steps)`：依相反順序執行 `.down.sql`（回復步驟與備份建議見 docs/adr/036）
- 整個流程用 advisory lock 序列化，多個行程同時啟動也只有一個在跑
- 只動背景工作系統自己的資料表；不碰 uploads、紀錄、知識庫與生產 SQLite
"""

import hashlib
from pathlib import Path

from app.jobs import db

DIR = Path(__file__).parent / "migrations"
LOCK_KEY = 7_036_001  # 任意固定整數：migration 專用的 advisory lock


class MigrationError(Exception):
    pass


def _files(suffix: str) -> list[Path]:
    return sorted(DIR.glob(f"*.{suffix}.sql"))


def _version(path: Path) -> str:
    return path.name.split("_", 1)[0]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )


def status() -> list[dict]:
    with db.connection() as conn:
        _ensure_table(conn)
        applied = {r["version"]: r for r in conn.execute("SELECT * FROM schema_migrations")}
    out = []
    for f in _files("up"):
        v = _version(f)
        row = applied.get(v)
        out.append(
            {
                "version": v,
                "name": f.name,
                "applied": row is not None,
                "changed": row is not None and row["checksum"] != _digest(f),
            }
        )
    return out


def apply() -> list[str]:
    done = []
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        _ensure_table(conn)
        applied = {
            r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM schema_migrations")
        }
        for f in _files("up"):
            v = _version(f)
            if v in applied:
                if applied[v] != _digest(f):
                    raise MigrationError(
                        f"{f.name} 已套用後又被修改，請新增一個 migration，不要改舊的"
                    )
                continue
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (v, f.name, _digest(f)),
            )
            done.append(f.name)
    return done


def rollback(steps: int = 1) -> list[str]:
    undone = []
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        _ensure_table(conn)
        applied = [
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC")
        ]
        downs = {_version(f): f for f in _files("down")}
        for v in applied[:steps]:
            f = downs.get(v)
            if f is None:
                raise MigrationError(f"版本 {v} 沒有對應的 .down.sql，無法回復")
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("DELETE FROM schema_migrations WHERE version = %s", (v,))
            undone.append(f.name)
    return undone
