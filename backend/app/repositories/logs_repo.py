"""使用紀錄與回饋（SQLite；.env 設了 DATABASE_URL 就改用 PostgreSQL 版 pg_logs_repo.py，
資料表欄位相同、查詢語句共用）。SQL 只寫在這層。

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
CREATE TABLE IF NOT EXISTS route_logs (
  request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, account_id TEXT, question TEXT,
  masked_text TEXT, has_photo INTEGER, engine TEXT, model TEXT, fallback_reason TEXT, intent TEXT,
  modify_op TEXT, confidence REAL, margin REAL, gate TEXT, overrides_rules INTEGER,
  egress_bytes INTEGER, system1_ms INTEGER, total_ms INTEGER
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, rating TEXT NOT NULL,
  note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, request_id TEXT,
  stage INTEGER NOT NULL, rule TEXT NOT NULL, judge TEXT, account_id TEXT, account_label TEXT,
  text TEXT
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
    "route_logs": {"outcome": "TEXT"},
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

    def add_route_log(self, row: dict) -> None:
        """智慧助理的路由紀錄：意圖、信心、閘門、第 2 段由誰判斷（Jev／地端規則）、
        外送位元組、結果（通過或在第幾段擋下）。"""
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        # 同一個 request_id 再寫一次就覆蓋；用 ON CONFLICT 而不是 SQLite 專用的 INSERT OR REPLACE，
        # PostgreSQL 版（pg_logs_repo）共用這段 SQL
        updates = ", ".join(f"{c} = excluded.{c}" for c in row if c != "request_id")
        self._exec(
            f"INSERT INTO route_logs ({cols}) VALUES ({marks})"
            f" ON CONFLICT (request_id) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def recent_routes(self, limit: int = 10) -> list[dict]:
        rows = self._exec(
            "SELECT request_id, created_at, account_id, question, masked_text, engine, model,"
            " fallback_reason, intent, modify_op, confidence, gate, egress_bytes, total_ms, outcome"
            " FROM route_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def add_security_log(self, row: dict) -> str:
        """五段防護的「拒絕並記錄」（docs/adr/012）：第 1 段 RBAC、第 2 段 Jev 護欄擋下的請求，
        第 4 段移除的夾帶指令段落。text 只存遮蔽個資後的文字。回傳紀錄編號（SEC-0001）。"""
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        rows = self._exec(
            f"INSERT INTO security_logs ({cols}) VALUES ({marks}) RETURNING id",
            tuple(row.values()),
        )
        return f"SEC-{rows[0]['id']:04d}"

    def recent_security(self, limit: int = 20) -> list[dict]:
        rows = self._exec(
            "SELECT id, created_at, request_id, stage, rule, judge, account_id, account_label, text"
            " FROM security_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{**dict(r), "no": f"SEC-{r['id']:04d}"} for r in rows]

    def clear_security(self) -> int:
        """展示還原：清掉拒絕並記錄的紀錄（評估腳本也會寫入），回傳清掉幾筆。"""
        n = self._exec("SELECT COUNT(*) AS n FROM security_logs")[0]["n"]
        self._exec("DELETE FROM security_logs")
        return int(n)

    def count_security(self, since: str) -> dict[int, int]:
        """since（ISO 時間）之後各段擋下或移除的筆數。"""
        rows = self._exec(
            "SELECT stage, COUNT(*) AS n FROM security_logs WHERE created_at >= ? GROUP BY stage",
            (since,),
        )
        return {int(r["stage"]): int(r["n"]) for r in rows}

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
        s = get_settings()
        if s.database_url:
            from app.repositories.pg_logs_repo import PgLogsRepo

            _repo = PgLogsRepo()
        else:
            _repo = LogsRepo(s.data_dir / "artrag.sqlite3")
    return _repo
