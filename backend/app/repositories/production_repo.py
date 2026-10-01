"""生產排程的可寫入資料（SQLite；正式版改 PostgreSQL 的 production schema）。SQL 只寫在這層。

- 使用者在圖紙頁開立的工單（既有工單在 kb/inventory 的 JSON，是唯讀的示範資料）
- 每次排程的結果（schedule_runs）與各工序的機台、起訖（schedule_ops）；
  最新一次完成的排程就是「目前排程」

這個檔案是寫入端；Text-to-SQL 查的工廠資料庫（inventory.sqlite3）在建庫時讀這裡的資料，
所以模型看得到新工單與排程結果，但模型產生的 SQL 永遠碰不到這個可寫入的檔案。
每次寫入都遞增 revision，工廠資料庫偵測到就自動重建（見 inventory_repo.seed_hash）。
"""

import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_orders (
  wo_no TEXT PRIMARY KEY, part_id TEXT NOT NULL, qty INTEGER NOT NULL, priority TEXT NOT NULL,
  release_on TEXT NOT NULL, due_on TEXT NOT NULL, note TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_runs (
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, request_id TEXT, engine TEXT NOT NULL,
  engine_version TEXT, status TEXT NOT NULL, seconds_limit INTEGER, solve_ms INTEGER,
  score TEXT, hard INTEGER, medium INTEGER, soft INTEGER, initial_score TEXT,
  improvements INTEGER, n_work_orders INTEGER, n_operations INTEGER,
  kpis TEXT, analysis TEXT, work_orders TEXT, note TEXT, score_check INTEGER
);
CREATE TABLE IF NOT EXISTS schedule_ops (
  run_id TEXT NOT NULL, op_id TEXT NOT NULL, wo_no TEXT NOT NULL, part_id TEXT NOT NULL,
  op_seq INTEGER NOT NULL, op_name TEXT NOT NULL, kind TEXT NOT NULL, machine_id TEXT,
  start_min INTEGER NOT NULL, end_min INTEGER NOT NULL, setup_min INTEGER NOT NULL,
  run_min INTEGER NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, pinned INTEGER NOT NULL,
  PRIMARY KEY (run_id, op_id)
);
"""

WO_NO = re.compile(r"^WO-(\d{4})-(\d{2,3})$")


class ProductionRepo:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    # 每次操作開一條連線：make demo-reset 刪掉檔案後會自動重建
    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # 舊版資料庫缺少的欄位（正式版改用 Alembic migration）
        have = {r["name"] for r in conn.execute("PRAGMA table_info(schedule_runs)")}
        if "score_check" not in have:
            conn.execute("ALTER TABLE schedule_runs ADD COLUMN score_check INTEGER")
        return conn

    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    def _bump(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta VALUES ('revision', '1') ON CONFLICT(key) DO UPDATE"
            " SET value = CAST(value AS INTEGER) + 1"
        )

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def revision(self) -> str:
        """資料版本：工廠資料庫用來判斷要不要重建。檔案不存在時為 '0'。"""
        if not self.path.is_file():
            return "0"
        rows = self._read("SELECT value FROM meta WHERE key = 'revision'")
        return rows[0]["value"] if rows else "0"

    # ------------------------------------------------------------ 工單
    def next_wo_no(self, yymm: str, taken: set[str]) -> str:
        """同月份的下一個工單號。

        taken 是知識庫（含 kb_staging）已用掉的單號，避免 make demo-add 撞號。
        """
        used = set(taken) | {r["wo_no"] for r in self._read("SELECT wo_no FROM work_orders")}
        n = max(
            (int(m.group(2)) for w in used if (m := WO_NO.match(w)) and m.group(1) == yymm),
            default=0,
        )
        return f"WO-{yymm}-{n + 1:02d}"

    def create_work_order(self, row: dict) -> dict:
        row = {**row, "status": "已開立", "created_at": self.now()}
        with self._lock:
            conn = self._conn()
            try:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO work_orders ({cols}) VALUES ({marks})", tuple(row.values())
                )
                self._bump(conn)
                conn.commit()
            finally:
                conn.close()
        return row

    def cancel_work_order(self, wo_no: str) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE work_orders SET status = '已取消'"
                    " WHERE wo_no = ? AND status = '已開立'",
                    (wo_no,),
                )
                if cur.rowcount:
                    self._bump(conn)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def work_orders(self, include_cancelled: bool = False) -> list[dict]:
        where = "" if include_cancelled else "WHERE status != '已取消'"
        return self._read(f"SELECT * FROM work_orders {where} ORDER BY created_at")

    # ------------------------------------------------------------ 排程結果
    def save_run(self, run: dict, ops: list[dict]) -> str:
        run_id = run.get("run_id") or "plan_" + uuid.uuid4().hex[:12]
        row = {
            **run,
            "run_id": run_id,
            "created_at": run.get("created_at") or self.now(),
            "kpis": json.dumps(run.get("kpis") or {}, ensure_ascii=False),
            "analysis": json.dumps(run.get("analysis") or [], ensure_ascii=False),
            "work_orders": json.dumps(run.get("work_orders") or [], ensure_ascii=False),
        }
        with self._lock:
            conn = self._conn()
            try:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO schedule_runs ({cols}) VALUES ({marks})", tuple(row.values())
                )
                conn.executemany(
                    "INSERT INTO schedule_ops (run_id, op_id, wo_no, part_id, op_seq, op_name,"
                    " kind,"
                    " machine_id, start_min, end_min, setup_min, run_min, start_at, end_at, pinned)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (run_id, o["op_id"], o["wo_no"], o["part_id"], o["op_seq"], o["op_name"],
                         o["kind"], o.get("machine_id"), o["start_min"], o["end_min"],
                         o.get("setup_min", 0), o.get("run_min", 0), o["start_at"], o["end_at"],
                         int(bool(o.get("pinned"))))
                        for o in ops
                    ],
                )  # fmt: skip
                self._bump(conn)
                conn.commit()
            finally:
                conn.close()
        return run_id

    @staticmethod
    def _decode(run: dict) -> dict:
        for k in ("kpis", "analysis", "work_orders"):
            run[k] = json.loads(run[k]) if run.get(k) else ([] if k != "kpis" else {})
        return run

    def latest_run(self) -> dict | None:
        """目前排程：最新一次完成（含提前結束）的排程。"""
        rows = self._read(
            "SELECT * FROM schedule_runs WHERE status IN ('done', 'stopped')"
            " ORDER BY created_at DESC LIMIT 1"
        )
        return self._decode(rows[0]) if rows else None

    def get_run(self, run_id: str) -> dict | None:
        rows = self._read("SELECT * FROM schedule_runs WHERE run_id = ?", (run_id,))
        return self._decode(rows[0]) if rows else None

    def run_ops(self, run_id: str) -> list[dict]:
        return self._read(
            "SELECT * FROM schedule_ops WHERE run_id = ? ORDER BY start_min, machine_id", (run_id,)
        )

    def recent_runs(self, limit: int = 10) -> list[dict]:
        rows = self._read(
            "SELECT run_id, created_at, engine, engine_version, status, seconds_limit, solve_ms,"
            " score, hard, medium, soft, initial_score, improvements, n_work_orders,"
            " n_operations, kpis, score_check FROM schedule_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [{**r, "kpis": json.loads(r["kpis"] or "{}")} for r in rows]

    def snapshot(self) -> dict:
        """給工廠資料庫建庫用：有效的使用者工單＋目前排程的各工序。"""
        run = self.latest_run()
        return {
            "work_orders": self.work_orders(),
            "run": run,
            "ops": self.run_ops(run["run_id"]) if run else [],
        }

    def reset(self) -> None:
        """展示還原：清掉使用者開立的工單與所有排程結果。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(
                    "DELETE FROM work_orders; DELETE FROM schedule_runs; DELETE FROM schedule_ops;"
                )
                self._bump(conn)
                conn.commit()
            finally:
                conn.close()

    def ping(self) -> bool:
        try:
            return bool(self._read("SELECT 1 AS ok"))
        except sqlite3.Error:
            return False


_repo: ProductionRepo | None = None


def get_production_repo() -> ProductionRepo:
    global _repo
    if _repo is None or _repo.path.parent != get_settings().data_dir:
        _repo = ProductionRepo(get_settings().data_dir / "production.sqlite3")
    return _repo
