"""生產排程的可寫入資料（SQLite；正式版改 PostgreSQL 的 production schema）。SQL 只寫在這層。

- 使用者在圖紙頁開立的工單（既有工單在 kb/inventory 的 JSON，是唯讀的示範資料）
- 每次排程的結果（schedule_runs）與各工序的機台、起訖（schedule_ops）；
  最新一次完成的排程就是「目前排程」
- 智慧助理的修改資料（docs/adr/011）：已寫入的異動（data_changes，盤點 IC-、調撥 TR-、報廢 SC-…）、
  超額送主管的待核准單（approvals，AP-）與稽核紀錄（audit_log，拒絕也記）

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
CREATE TABLE IF NOT EXISTS data_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, change_no TEXT NOT NULL UNIQUE, op TEXT NOT NULL,
  params TEXT NOT NULL, summary TEXT NOT NULL, moved_on TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_label TEXT NOT NULL, approval_no TEXT, approver_label TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  ap_no TEXT PRIMARY KEY, op TEXT NOT NULL, params TEXT NOT NULL, summary TEXT NOT NULL,
  reasons TEXT NOT NULL, diff TEXT NOT NULL, fingerprint TEXT NOT NULL, note TEXT,
  requester_id TEXT NOT NULL, requester_label TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL, decided_by TEXT, decided_label TEXT, decided_at TEXT, decision_note TEXT,
  change_no TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_label TEXT NOT NULL, action TEXT NOT NULL, op TEXT, ref_no TEXT, summary TEXT,
  detail TEXT, request_id TEXT
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
        have = {r["name"] for r in conn.execute("PRAGMA table_info(work_orders)")}
        if "created_by" not in have:  # 開立人：生管只能取消自己開的工單
            conn.execute("ALTER TABLE work_orders ADD COLUMN created_by TEXT")
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

    def get_work_order(self, wo_no: str) -> dict | None:
        rows = self._read("SELECT * FROM work_orders WHERE wo_no = ?", (wo_no,))
        return rows[0] if rows else None

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
        """給工廠資料庫建庫用：有效的使用者工單＋目前排程的各工序＋已寫入的異動（依序重播）。"""
        run = self.latest_run()
        return {
            "work_orders": self.work_orders(),
            "run": run,
            "ops": self.run_ops(run["run_id"]) if run else [],
            "changes": self.changes(),
        }

    # ------------------------------------------------------------ 修改資料（智慧助理）
    def changes(self, limit: int | None = None) -> list[dict]:
        """已寫入的異動，依寫入順序（工廠資料庫重建時照這個順序重播）。"""
        sql = "SELECT * FROM data_changes ORDER BY seq"
        if limit:
            sql = f"SELECT * FROM (SELECT * FROM data_changes ORDER BY seq DESC LIMIT {int(limit)})"
            sql += " ORDER BY seq DESC"
        return [{**r, "params": json.loads(r["params"])} for r in self._read(sql)]

    def doc_numbers(self) -> set[str]:
        """已用掉的單號（異動、待核准），產生新單號時避開。"""
        rows = self._read(
            "SELECT change_no AS no FROM data_changes UNION SELECT ap_no FROM approvals"
        )
        return {r["no"] for r in rows}

    def add_change(self, row: dict, approval: dict | None = None) -> dict:
        """寫入一筆異動（和待核准單的狀態更新在同一個交易裡）並遞增資料版本。"""
        row = {
            **row,
            "params": json.dumps(row["params"], ensure_ascii=False),
            "created_at": self.now(),
        }
        with self._lock:
            conn = self._conn()
            try:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO data_changes ({cols}) VALUES ({marks})", tuple(row.values())
                )
                if approval:
                    conn.execute(
                        "UPDATE approvals SET status = '已核准', decided_by = ?, decided_label = ?,"
                        " decided_at = ?, decision_note = ?, change_no = ? WHERE ap_no = ?",
                        (approval["decided_by"], approval["decided_label"], self.now(),
                         approval.get("decision_note"), row["change_no"], approval["ap_no"]),
                    )  # fmt: skip
                self._bump(conn)
                conn.commit()
            finally:
                conn.close()
        return {**row, "params": json.loads(row["params"])}

    def add_approval(self, row: dict) -> dict:
        row = {
            **row,
            **{k: json.dumps(row[k], ensure_ascii=False) for k in ("params", "reasons", "diff")},
            "status": "待核准",
            "created_at": self.now(),
        }
        with self._lock:
            conn = self._conn()
            try:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO approvals ({cols}) VALUES ({marks})", tuple(row.values())
                )
                conn.commit()
            finally:
                conn.close()
        return self.get_approval(row["ap_no"])  # type: ignore[return-value]

    @staticmethod
    def _decode_approval(r: dict) -> dict:
        return {**r, **{k: json.loads(r[k]) for k in ("params", "reasons", "diff")}}

    def get_approval(self, ap_no: str) -> dict | None:
        rows = self._read("SELECT * FROM approvals WHERE ap_no = ?", (ap_no,))
        return self._decode_approval(rows[0]) if rows else None

    def approvals(self, status: str | None = None, requester_id: str | None = None,
                  limit: int = 50) -> list[dict]:  # fmt: skip
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if requester_id:
            where.append("requester_id = ?")
            params.append(requester_id)
        sql = "SELECT * FROM approvals" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [self._decode_approval(r) for r in self._read(sql, (*params, limit))]

    def decide_approval(
        self,
        ap_no: str,
        status: str,
        by: str,
        label: str,
        note: str | None,
        change_no: str | None = None,
    ) -> bool:
        """退回、失效、核准（異動類的核准走 add_change，和寫入同一個交易）。只改「待核准」的單。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE approvals SET status = ?, decided_by = ?, decided_label = ?,"
                    " decided_at = ?, decision_note = ?, change_no = ?"
                    " WHERE ap_no = ? AND status = '待核准'",
                    (status, by, label, self.now(), note, change_no, ap_no),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def expire_approvals(self, before_iso: str) -> list[str]:
        rows = self._read(
            "SELECT ap_no FROM approvals WHERE status = '待核准' AND created_at < ?", (before_iso,)
        )
        for r in rows:
            self.decide_approval(r["ap_no"], "已失效", "system", "系統", "超過 24 小時沒人處理")
        return [r["ap_no"] for r in rows]

    def add_audit(self, row: dict) -> None:
        row = {**row, "at": self.now()}
        if isinstance(row.get("detail"), (dict, list)):
            row["detail"] = json.dumps(row["detail"], ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                conn.execute(
                    f"INSERT INTO audit_log ({cols}) VALUES ({marks})", tuple(row.values())
                )
                conn.commit()
            finally:
                conn.close()

    def audit(self, limit: int = 50) -> list[dict]:
        rows = self._read("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            if r.get("detail") and r["detail"][:1] in "[{":
                r["detail"] = json.loads(r["detail"])
        return rows

    def reset(self) -> None:
        """展示還原：清掉使用者開立的工單、所有排程結果、智慧助理的異動、待核准單與稽核紀錄。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(
                    "DELETE FROM work_orders; DELETE FROM schedule_runs; DELETE FROM schedule_ops;"
                    " DELETE FROM data_changes; DELETE FROM approvals; DELETE FROM audit_log;"
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
