"""使用紀錄與回饋（SQLite；正式版改 PostgreSQL，資料表欄位相同）。SQL 只寫在這層。

庫存資料（Text-to-SQL 查詢的對象）在另一個檔案，見 inventory_repo.py。
"""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  image_id TEXT PRIMARY KEY, path TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_logs (
  request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, artwork_id TEXT, question TEXT,
  strategy_requested TEXT, strategy_used TEXT, model TEXT, prompt_version TEXT,
  use_retrieval INTEGER, fallback INTEGER, retrieval_ms INTEGER, first_token_ms INTEGER,
  total_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_twd REAL,
  egress_images INTEGER DEFAULT 0, egress_chunks INTEGER DEFAULT 0, egress_bytes INTEGER DEFAULT 0,
  answer TEXT
);
CREATE TABLE IF NOT EXISTS cad_logs (
  request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, job_id TEXT, part_id TEXT, image_id TEXT,
  strategy TEXT, model TEXT, ok INTEGER, error TEXT, iou REAL, iou_bbox REAL, scale REAL,
  scale_source TEXT,
  first_token_ms INTEGER, generation_ms INTEGER, exec_ms INTEGER, total_ms INTEGER,
  output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS sql_logs (
  request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, question TEXT, strategy_requested TEXT,
  strategy_used TEXT, model TEXT, prompt_version TEXT, sql TEXT, ok INTEGER, error TEXT,
  attempts INTEGER, row_count INTEGER, sql_ms INTEGER, exec_ms INTEGER, total_ms INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, answer TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, rating TEXT NOT NULL,
  note TEXT, created_at TEXT NOT NULL
);
"""

# 舊版資料庫缺少的欄位：啟動時補上（正式版改用 Alembic migration）
_ADDED_COLUMNS = {
    "chat_logs": {
        "egress_images": "INTEGER DEFAULT 0",
        "egress_chunks": "INTEGER DEFAULT 0",
        "egress_bytes": "INTEGER DEFAULT 0",
        "part_id": "TEXT",
    },
    "cad_logs": {"iou_bbox": "REAL"},
}


class LogsRepo:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._conn.commit()
        self._lock = threading.Lock()

    def _exec(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            self._conn.commit()
            return rows

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def add_upload(self, image_id: str, path: str) -> None:
        self._exec("INSERT INTO uploads VALUES (?, ?, ?)", (image_id, path, self.now()))

    def get_upload(self, image_id: str) -> str | None:
        rows = self._exec("SELECT path FROM uploads WHERE image_id = ?", (image_id,))
        return rows[0]["path"] if rows else None

    def purge_uploads(self, ttl_days: int) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).isoformat()
        rows = self._exec("SELECT path FROM uploads WHERE created_at < ?", (cutoff,))
        self._exec("DELETE FROM uploads WHERE created_at < ?", (cutoff,))
        return [r["path"] for r in rows]

    def add_chat_log(self, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._exec(f"INSERT INTO chat_logs ({cols}) VALUES ({marks})", tuple(row.values()))

    def recent_chats(self, limit: int = 20) -> list[dict]:
        rows = self._exec(
            "SELECT request_id, created_at, artwork_id, question, strategy_requested,"
            " strategy_used, model, fallback, first_token_ms, total_ms, cost_twd,"
            " egress_images, egress_chunks, egress_bytes FROM chat_logs"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def add_cad_log(self, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._exec(f"INSERT INTO cad_logs ({cols}) VALUES ({marks})", tuple(row.values()))

    def recent_cad(self, limit: int = 10) -> list[dict]:
        rows = self._exec("SELECT * FROM cad_logs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def recent_cad_for_part(self, part_id: str, limit: int = 10) -> list[dict]:
        rows = self._exec(
            "SELECT job_id, created_at, strategy, model, ok, iou, iou_bbox, total_ms FROM cad_logs"
            " WHERE part_id = ? AND image_id IS NULL ORDER BY created_at DESC LIMIT ?",
            (part_id, limit),
        )
        return [dict(r) for r in rows]

    def add_sql_log(self, row: dict) -> None:
        """Text-to-SQL 紀錄：產生的 SQL、修正次數、筆數與延遲（評估與除錯用）。"""
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._exec(f"INSERT INTO sql_logs ({cols}) VALUES ({marks})", tuple(row.values()))

    def recent_sql(self, limit: int = 10) -> list[dict]:
        rows = self._exec(
            "SELECT request_id, created_at, question, strategy_used, model, sql, ok, error,"
            " attempts, row_count, total_ms FROM sql_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def add_feedback(self, request_id: str, rating: str, note: str | None) -> None:
        self._exec(
            "INSERT INTO feedback (request_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
            (request_id, rating, note, self.now()),
        )

    def ping(self) -> bool:
        return bool(self._exec("SELECT 1"))


_repo: LogsRepo | None = None


def get_logs_repo() -> LogsRepo:
    global _repo
    if _repo is None:
        _repo = LogsRepo(get_settings().data_dir / "artrag.sqlite3")
    return _repo
