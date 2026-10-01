"""工廠資料庫（Text-to-SQL 的查詢對象）：SQLite；正式版改 PostgreSQL 的唯讀角色，資料表相同。

資料來源是 kb/inventory/ 的 JSON（一張圖紙一個檔）、kb/parts/ 的圖紙主檔、kb/production/ 的機台，
再加上 production.sqlite3 裡使用者開立的工單與目前排程（生產排程，見 production_repo.py）；
任一來源有變動時自動重建 data/inventory.sqlite3（與使用紀錄 artrag.sqlite3 分開，
模型產生的 SQL 碰不到使用紀錄，也碰不到可寫入的 production.sqlite3）。SQL 只寫在這層。

模型產生的 SQL 由 run_readonly() 執行，三道保護：
1. 唯讀開檔（mode=ro）＋ PRAGMA query_only
2. authorizer 白名單：只准 SELECT、只准讀下列資料表與檢視表、只准呼叫白名單函式
3. 執行時間上限（progress handler）與回傳列數上限
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import REPO_ROOT, get_settings
from app.core.logging import log

# 改了資料表定義就遞增，已建好的資料庫會自動重建
SCHEMA_VERSION = 2

# (資料表, 說明, [(欄位, 型別與限制, 說明)])
# 建表 DDL、給模型的 schema、/inventory/schema 都從這裡產生
TABLES: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "parts",
        "零件主檔（一張圖紙一個料號）",
        [
            ("part_id", "TEXT PRIMARY KEY", "圖紙 ID，例如 'mfg-001'"),
            ("part_no", "TEXT NOT NULL UNIQUE", "料號，例如 'BRK-1001'"),
            ("drawing_no", "TEXT NOT NULL", "圖號"),
            ("revision", "TEXT NOT NULL", "版次"),
            ("name", "TEXT NOT NULL", "品名，例如 'L 型固定支架'"),
            ("category", "TEXT NOT NULL", "類別"),
            ("material", "TEXT NOT NULL", "材料"),
            ("confidentiality", "TEXT NOT NULL", "機密等級：公開／內部／機密"),
            ("unit", "TEXT", "單位"),
            ("std_cost_twd", "REAL", "標準成本（元／件）"),
            ("safety_stock", "INTEGER", "安全庫存量（件，和「可用」庫存比較）"),
            ("reorder_qty", "INTEGER", "建議補貨批量（件）"),
            ("lead_time_days", "INTEGER", "生產前置天數"),
            ("make_or_buy", "TEXT", "自製／委外方式"),
        ],
    ),
    (
        "warehouses",
        "倉庫",
        [
            ("warehouse_id", "TEXT PRIMARY KEY", "倉庫代碼，例如 'WH-A'"),
            ("name", "TEXT NOT NULL", "倉庫名稱"),
            ("kind", "TEXT NOT NULL", "類型：成品／待檢／隔離"),
            ("site", "TEXT NOT NULL", "廠區：一廠／二廠"),
        ],
    ),
    (
        "stock",
        "目前庫存（依倉庫、儲位、批號、狀態分列，同一零件有多列）",
        [
            ("stock_id", "INTEGER PRIMARY KEY", ""),
            ("part_id", "TEXT NOT NULL REFERENCES parts(part_id)", ""),
            ("warehouse_id", "TEXT NOT NULL REFERENCES warehouses(warehouse_id)", ""),
            ("bin", "TEXT NOT NULL", "儲位"),
            ("lot_no", "TEXT NOT NULL", "批號"),
            ("status", "TEXT NOT NULL", "狀態：可用／保留（已保留給訂單）／待檢／不良"),
            ("qty", "INTEGER NOT NULL", "數量（件）"),
            ("received_on", "TEXT NOT NULL", "入庫日期 YYYY-MM-DD"),
            ("note", "TEXT", "備註：保留對象、不良原因"),
        ],
    ),
    (
        "stock_moves",
        "庫存異動紀錄",
        [
            ("move_id", "INTEGER PRIMARY KEY", ""),
            ("moved_on", "TEXT NOT NULL", "異動日期 YYYY-MM-DD"),
            ("part_id", "TEXT NOT NULL REFERENCES parts(part_id)", ""),
            ("warehouse_id", "TEXT NOT NULL REFERENCES warehouses(warehouse_id)", ""),
            (
                "move_type",
                "TEXT NOT NULL",
                "期初／生產入庫／銷貨出庫／調撥入／調撥出／報廢／盤點調整",
            ),
            ("qty", "INTEGER NOT NULL", "數量：正數入庫、負數出庫"),
            ("ref_no", "TEXT", "單號：工單 WO-、訂單 SO-、調撥 TR-、報廢 SC-、盤點 IC-"),
            ("note", "TEXT", "備註"),
        ],
    ),
    (
        "work_orders",
        "生產工單（含圖紙頁開立的新工單）",
        [
            ("wo_no", "TEXT PRIMARY KEY", "工單號"),
            ("part_id", "TEXT NOT NULL REFERENCES parts(part_id)", ""),
            ("qty_planned", "INTEGER NOT NULL", "計畫數量"),
            ("qty_done", "INTEGER NOT NULL", "已完工數量"),
            ("status", "TEXT NOT NULL", "已開立／生產中／委外處理中／已完工"),
            ("priority", "TEXT NOT NULL", "一般／急件"),
            (
                "line",
                "TEXT NOT NULL",
                "主要產線／機台（machines.machine_id）；新工單尚未排程為 '待排程'",
            ),
            ("start_on", "TEXT NOT NULL", "開工日"),
            ("due_on", "TEXT NOT NULL", "預計完工日（交期）"),
            ("note", "TEXT", "備註"),
        ],
    ),
    (
        "machines",
        "機台",
        [
            ("machine_id", "TEXT PRIMARY KEY", "機台代碼，例如 'VMC-01'、'CNC 車床-01'"),
            ("name", "TEXT NOT NULL", "機台名稱"),
            (
                "machine_type",
                "TEXT NOT NULL",
                "機型：下料／車削／綜合加工／銑削／鑽孔攻牙／外圓研磨／平面研磨／鉗工",
            ),
            ("site", "TEXT NOT NULL", "廠區"),
        ],
    ),
    (
        "schedule_ops",
        "目前的生產排程（最新一次 Timefold 排程結果）：未完工工單每道工序的機台與起訖時間",
        [
            ("wo_no", "TEXT NOT NULL REFERENCES work_orders(wo_no)", ""),
            ("op_seq", "INTEGER NOT NULL", "工序號（10、20…）"),
            ("op_name", "TEXT NOT NULL", "工序名稱"),
            ("kind", "TEXT NOT NULL", "自製／委外（委外不佔機台）"),
            ("machine_id", "TEXT REFERENCES machines(machine_id)", "排到的機台；委外為 NULL"),
            ("start_at", "TEXT NOT NULL", "開始時間 'YYYY-MM-DD HH:MM'"),
            ("end_at", "TEXT NOT NULL", "結束時間 'YYYY-MM-DD HH:MM'"),
            ("setup_min", "INTEGER NOT NULL", "換線準備分鐘"),
            ("run_min", "INTEGER NOT NULL", "加工分鐘（委外為等待分鐘）"),
        ],
    ),
    (
        "sales_orders",
        "客戶訂單（一列一個品項）",
        [
            ("so_no", "TEXT NOT NULL", "訂單號"),
            ("line_no", "INTEGER NOT NULL", "項次"),
            ("customer", "TEXT NOT NULL", "客戶"),
            ("part_id", "TEXT NOT NULL REFERENCES parts(part_id)", ""),
            ("qty", "INTEGER NOT NULL", "訂購數量"),
            ("qty_shipped", "INTEGER NOT NULL", "已出貨數量；未出貨＝qty - qty_shipped"),
            ("order_on", "TEXT NOT NULL", "下單日"),
            ("due_on", "TEXT NOT NULL", "交期"),
            ("status", "TEXT NOT NULL", "待出貨／部分出貨／已出貨"),
        ],
    ),
]
TABLE_KEYS = {
    "sales_orders": "PRIMARY KEY (so_no, line_no)",
    "schedule_ops": "PRIMARY KEY (wo_no, op_seq)",
}

VIEWS: list[tuple[str, str, list[tuple[str, str]], str]] = [
    (
        "v_part_stock",
        "各零件庫存彙總（檢視表）",
        [
            ("part_id", ""),
            ("part_no", ""),
            ("name", "品名"),
            ("available", "可用"),
            ("reserved", "保留"),
            ("inspecting", "待檢"),
            ("defective", "不良"),
            ("on_hand", "合計（所有狀態）"),
            ("safety_stock", "安全庫存量"),
        ],
        """SELECT p.part_id, p.part_no, p.name,
  COALESCE(SUM(CASE WHEN s.status = '可用' THEN s.qty END), 0) AS available,
  COALESCE(SUM(CASE WHEN s.status = '保留' THEN s.qty END), 0) AS reserved,
  COALESCE(SUM(CASE WHEN s.status = '待檢' THEN s.qty END), 0) AS inspecting,
  COALESCE(SUM(CASE WHEN s.status = '不良' THEN s.qty END), 0) AS defective,
  COALESCE(SUM(s.qty), 0) AS on_hand,
  p.safety_stock
FROM parts p LEFT JOIN stock s ON s.part_id = p.part_id
GROUP BY p.part_id""",
    ),
    (
        "v_wo_plan",
        "未完工工單的排程摘要（檢視表）",
        [
            ("wo_no", ""),
            ("part_id", ""),
            ("name", "品名"),
            ("priority", "一般／急件"),
            ("qty_remaining", "未完工數量"),
            ("due_on", "交期"),
            ("planned_start", "排程開工時間"),
            ("planned_end", "排程完工時間（含委外）"),
            ("on_time", "準時／延遲／未排程"),
        ],
        """SELECT w.wo_no, w.part_id, p.name, w.priority,
  w.qty_planned - w.qty_done AS qty_remaining,
  w.due_on, MIN(s.start_at) AS planned_start, MAX(s.end_at) AS planned_end,
  CASE WHEN MAX(s.end_at) IS NULL THEN '未排程'
       WHEN substr(MAX(s.end_at), 1, 10) <= w.due_on THEN '準時' ELSE '延遲' END AS on_time
FROM work_orders w JOIN parts p ON p.part_id = w.part_id
LEFT JOIN schedule_ops s ON s.wo_no = w.wo_no
WHERE w.status != '已完工'
GROUP BY w.wo_no""",
    ),
]

ALLOWED_TABLES = frozenset([t[0] for t in TABLES] + [v[0] for v in VIEWS])
# 聚合、數值、字串、日期與視窗函式；其他函式（例如 load_extension）一律拒絕
_FUNCTIONS = """
count sum total avg min max group_concat string_agg abs round coalesce ifnull nullif iif
length lower upper substr substring trim ltrim rtrim replace instr printf format like glob
date time datetime julianday strftime unixepoch typeof
row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value
"""
ALLOWED_FUNCTIONS = frozenset(_FUNCTIONS.split())
STATUSES = ["可用", "保留", "待檢", "不良"]


class SqlError(Exception):
    """模型產生的 SQL 無法執行（語法、欄位不存在、未授權、逾時）；訊息會回饋給模型修正。"""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool
    exec_ms: int


def _ddl() -> str:
    parts = []
    for name, _, cols in TABLES:
        lines = [f"  {c} {decl}" for c, decl, _ in cols]
        if name in TABLE_KEYS:
            lines.append(f"  {TABLE_KEYS[name]}")
        parts.append(f"CREATE TABLE {name} (\n" + ",\n".join(lines) + "\n);")
    parts += [f"CREATE VIEW {name} AS\n{sql};" for name, _, _, sql in VIEWS]
    parts += [
        "CREATE INDEX idx_stock_part ON stock(part_id);",
        "CREATE INDEX idx_moves_part ON stock_moves(part_id, moved_on);",
        "CREATE INDEX idx_so_part ON sales_orders(part_id);",
        "CREATE INDEX idx_sched_machine ON schedule_ops(machine_id, start_at);",
    ]
    return "\n".join(parts)


def prompt_schema() -> str:
    """給模型看的 schema：DDL 加中文註解（型別只留基本型別，省 token）。"""
    out = []
    for name, desc, cols in TABLES:
        out.append(f"CREATE TABLE {name} (  -- {desc}")
        lines = []
        for c, decl, note in cols:
            basic = decl.split()[0] + (" PRIMARY KEY" if "PRIMARY KEY" in decl else "")
            ref = f"→ {decl.split('REFERENCES ')[1]}" if "REFERENCES" in decl else ""
            comment = " ".join(x for x in (note, ref) if x)
            lines.append((f"  {c} {basic}", f"  -- {comment}" if comment else ""))
        if name in TABLE_KEYS:
            lines.append((f"  {TABLE_KEYS[name]}", ""))
        out += [f"{d}{',' if i < len(lines) - 1 else ''}{c}" for i, (d, c) in enumerate(lines)]
        out.append(");")
    for name, desc, cols, _ in VIEWS:
        col_text = ", ".join(f"{c}（{n}）" if n else c for c, n in cols)
        out.append(f"-- 檢視表 {name}：{desc}，欄位 {col_text}")
    return "\n".join(out)


def _seed_files() -> list[Path]:
    kb = REPO_ROOT / "kb"
    return [
        kb / "inventory" / "site.json",
        *sorted((kb / "inventory" / "items").glob("*.json")),
        *sorted((kb / "parts").glob("*.json")),
        kb / "production" / "site.json",
    ]


def seed_hash() -> str:
    """知識庫 JSON＋production.sqlite3 的資料版本：任一變動就重建。"""
    from app.repositories.production_repo import get_production_repo

    h = hashlib.sha256(f"schema-v{SCHEMA_VERSION}".encode())
    for p in _seed_files():
        if p.is_file():
            h.update(p.relative_to(REPO_ROOT).as_posix().encode())
            h.update(p.read_bytes())
    h.update(f"production-r{get_production_repo().revision()}".encode())
    return h.hexdigest()[:16]


def _user_work_orders(production: dict) -> list[tuple]:
    """圖紙頁開立的工單：主要機台與開工日取自目前排程（加工最久的那道工序），還沒排就是「待排程」。"""
    ops = production.get("ops", [])
    rows = []
    for w in production.get("work_orders", []):
        mine = [o for o in ops if o["wo_no"] == w["wo_no"]]
        made = [o for o in mine if o["kind"] == "自製"]
        main = max(made, key=lambda o: o["run_min"], default=None)
        rows.append(
            (w["wo_no"], w["part_id"], w["qty"], 0, "已開立", w["priority"],
             main["machine_id"] if main else "待排程",
             min(o["start_at"] for o in mine)[:10] if mine else w["release_on"], w["due_on"],
             w.get("note") or "圖紙頁開立")
        )  # fmt: skip
    return rows


def build_db(
    path: Path, site: dict, parts: list[dict], items: list[dict], production: dict | None = None
) -> dict[str, int]:
    """寫到暫存檔再換上，查詢中的連線不受影響。回傳各資料表筆數。

    production：{"machines": kb/production 的機台, "work_orders": 使用者開立的工單, "ops": 目前排程,
                 "changes": 智慧助理寫入的異動（依序重播）}
    """
    production = production or {}
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(_ddl())
        inv = {it["part_id"]: it for it in items}
        for p in parts:
            it = inv.get(p["id"], {})
            conn.execute(
                "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["id"], p["part_no"], p["drawing_no"], p["revision"], p["name"]["zh"],
                    p["category"], p["material"], p["confidentiality"], it.get("unit"),
                    it.get("std_cost_twd"), it.get("safety_stock"), it.get("reorder_qty"),
                    it.get("lead_time_days"), it.get("make_or_buy"),
                ),
            )  # fmt: skip
        conn.executemany(
            "INSERT INTO warehouses VALUES (?,?,?,?)",
            [(w["warehouse_id"], w["name"], w["kind"], w["site"]) for w in site["warehouses"]],
        )
        moves = []
        for it in items:
            pid = it["part_id"]
            conn.executemany(
                "INSERT INTO stock (part_id, warehouse_id, bin, lot_no, status, qty, received_on,"
                " note) VALUES (?,?,?,?,?,?,?,?)",
                [
                    (pid, s["warehouse_id"], s["bin"], s["lot_no"], s["status"], s["qty"],
                     s["received_on"], s.get("note"))
                    for s in it["stock"]
                ],
            )  # fmt: skip
            moves += [(m["moved_on"], pid, i, m) for i, m in enumerate(it["moves"])]
            conn.executemany(
                "INSERT INTO work_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (w["wo_no"], pid, w["qty_planned"], w["qty_done"], w["status"], "一般",
                     w["line"], w["start_on"], w["due_on"], w.get("note"))
                    for w in it["work_orders"]
                ],
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO sales_orders VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (s["so_no"], s["line_no"], s["customer"], pid, s["qty"], s["qty_shipped"],
                     s["order_on"], s["due_on"], s["status"])
                    for s in it["sales_orders"]
                ],
            )  # fmt: skip
        # 異動依日期排序編號，move_id 越大越新
        conn.executemany(
            "INSERT INTO stock_moves (moved_on, part_id, warehouse_id, move_type, qty, ref_no,"
            " note) VALUES (?,?,?,?,?,?,?)",
            [
                (m["moved_on"], pid, m["warehouse_id"], m["move_type"], m["qty"], m.get("ref_no"),
                 m.get("note"))
                for _, pid, _, m in sorted(moves, key=lambda x: x[:3])
            ],
        )  # fmt: skip
        # 生產排程：機台、使用者開立的工單、目前排程（工單已不存在的排程列略過）
        conn.executemany(
            "INSERT INTO machines VALUES (?,?,?,?)",
            [
                (m["machine_id"], m["name"], m["machine_type"], m["site"])
                for m in production.get("machines", [])
            ],
        )
        part_ids = {p["id"] for p in parts}
        conn.executemany(
            "INSERT OR IGNORE INTO work_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
            [r for r in _user_work_orders(production) if r[1] in part_ids],
        )
        known = {r[0] for r in conn.execute("SELECT wo_no FROM work_orders")}
        _replay_changes(conn, production.get("changes", []))
        conn.executemany(
            "INSERT OR IGNORE INTO schedule_ops VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (o["wo_no"], o["op_seq"], o["op_name"], o["kind"], o["machine_id"], o["start_at"],
                 o["end_at"], o["setup_min"], o["run_min"])
                for o in production.get("ops", [])
                if o["wo_no"] in known
            ],
        )  # fmt: skip
        conn.commit()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t, _, _ in TABLES}
    finally:
        conn.close()
    os.replace(tmp, path)
    return counts


def _replay_changes(conn: sqlite3.Connection, changes: list[dict]) -> None:
    """智慧助理寫入的異動（production.sqlite3 的 data_changes）依序重播；每筆各自一個 savepoint，
    某筆因知識庫改了而不再成立（例如庫存不夠扣）就跳過並記錄，不影響其他筆。"""
    from app.repositories.data_changes import REPLAYED, ChangeError, apply

    for ch in changes:
        if ch["op"] not in REPLAYED:
            continue
        conn.execute("SAVEPOINT change")
        try:
            apply(conn, ch["op"], ch["params"], ch["change_no"], ch["moved_on"])
            conn.execute("RELEASE change")
        except (ChangeError, sqlite3.Error) as e:
            conn.execute("ROLLBACK TO change")
            conn.execute("RELEASE change")
            log.error(f"異動 {ch['change_no']} 無法重播，已略過：{e}")


def _authorizer(action, arg1, arg2, db, _source):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # db 為 None 是 WITH 子查詢（CTE）的名稱；實體資料表（main）必須在白名單內
        if db is None and not (arg1 or "").lower().startswith("sqlite_"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if db == "main" and arg1 in ALLOWED_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return (
            sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class InventoryRepo:
    def __init__(self, path: Path):
        self.path = path
        self.manifest_path = path.with_name("inventory.manifest.json")
        self._lock = threading.Lock()
        self._checked_hash = ""
        self._checked_at = 0.0
        self.manifest: dict = {}
        self.problems: list[str] = []

    # ------------------------------------------------------------ 建庫
    def ensure_built(self, force: bool = False) -> None:
        """kb/inventory、kb/parts、kb/production 或 production.sqlite3 有變動就重建
        （make demo-add、開立工單、排程完成後不必重啟後端）。

        每秒最多檢查一次，避免每個查詢都重算雜湊；force=True 立刻檢查（寫入後呼叫）。
        """
        now = time.monotonic()
        if not force and now - self._checked_at < 1.0 and self.path.is_file():
            return
        self._checked_at = now
        current = seed_hash()
        if current == self._checked_hash and self.path.is_file():
            return
        with self._lock:
            if current == self._checked_hash and self.path.is_file():
                return
            if self.manifest_path.is_file() and not self.manifest:
                self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if self.path.is_file() and self.manifest.get("seed_hash") == current:
                self._checked_hash = current
                return
            self.rebuild(current)

    def rebuild(self, current: str | None = None) -> dict:
        from app.rag.kb import validate_inventory, validate_parts, validate_production
        from app.repositories.production_repo import get_production_repo

        current = current or seed_hash()
        parts, part_errors = validate_parts(check_drawing=False)
        site, items, errors = validate_inventory({p["id"] for p in parts})
        prod_site, _, prod_errors = validate_production({p["id"] for p in parts}, items)
        self.problems = part_errors + errors + prod_errors
        self._checked_hash = current
        if errors or site is None:
            log.error(f"庫存資料有誤，沿用舊資料庫：{errors[:3]}")
            return self.manifest
        self.path.parent.mkdir(parents=True, exist_ok=True)
        production = get_production_repo().snapshot()
        production["machines"] = prod_site["machines"] if prod_site else []
        counts = build_db(self.path, site, parts, items, production)
        self.manifest = {
            "seed_hash": current,
            "schema_version": SCHEMA_VERSION,
            "as_of": site["as_of"],
            "company": site["company"],
            "tables": counts,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"庫存資料庫已重建：{counts}")
        return self.manifest

    @property
    def as_of(self) -> str:
        return self.manifest.get("as_of", "")

    # ------------------------------------------------------------ 連線
    def _connect_ro(self) -> sqlite3.Connection:
        self.ensure_built()
        conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """系統自己的固定查詢（非模型產生）。"""
        conn = self._connect_ro()
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """系統自己的固定查詢（智慧助理推定倉庫、讀回寫入結果）；唯讀連線，不經模型。"""
        return self._query(sql, params)

    def copy_to_memory(self) -> sqlite3.Connection:
        """把工廠資料庫複製到記憶體（修改資料的試算：在交易內套用、讀出差異後回滾）。"""
        src = self._connect_ro()
        mem = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            src.backup(mem)
        finally:
            src.close()
        return mem

    def run_readonly(self, sql: str, max_rows: int, timeout_ms: int) -> QueryResult:
        """執行模型產生的 SQL（呼叫前已通過 text2sql.check_sql 的靜態檢查）。"""
        conn = self._connect_ro()
        conn.set_authorizer(_authorizer)
        deadline = time.perf_counter() + timeout_ms / 1000
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 2000)
        t0 = time.perf_counter()
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in cur.description or []]
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "not authorized" in msg:
                msg = "not authorized：只能讀取 schema 中列出的資料表，且只能用 SELECT"
            elif "interrupted" in msg:
                msg = f"執行超過 {timeout_ms} ms 被中止，請簡化查詢"
            raise SqlError(msg) from e
        finally:
            conn.close()
        return QueryResult(
            columns=columns,
            rows=[list(r) for r in rows[:max_rows]],
            truncated=len(rows) > max_rows,
            exec_ms=round((time.perf_counter() - t0) * 1000),
        )

    # ------------------------------------------------------------ 固定查詢（頁面用）
    def schema_info(self) -> dict:
        counts = self.manifest.get("tables", {})
        tables = [
            {
                "name": name,
                "description": desc,
                "kind": "table",
                "rows": counts.get(name),
                "columns": [
                    {"name": c, "type": decl.split()[0], "description": n} for c, decl, n in cols
                ],
            }
            for name, desc, cols in TABLES
        ]
        tables += [
            {
                "name": name,
                "description": desc,
                "kind": "view",
                "rows": None,
                "columns": [{"name": c, "type": "", "description": n} for c, n in cols],
            }
            for name, desc, cols, _ in VIEWS
        ]
        return {"as_of": self.as_of, "company": self.manifest.get("company", ""), "tables": tables}

    def value_hints(self) -> dict:
        """給模型的欄位值：零件、倉庫、客戶與各狀態欄的可能值（值連結）。"""
        return {
            "parts": self._query("SELECT part_id, part_no, name FROM parts ORDER BY part_id"),
            "warehouses": self._query(
                "SELECT warehouse_id, name, kind FROM warehouses ORDER BY warehouse_id"
            ),
            "customers": [
                r["customer"]
                for r in self._query("SELECT DISTINCT customer FROM sales_orders ORDER BY customer")
            ],
            "machines": self._query(
                "SELECT machine_id, machine_type FROM machines ORDER BY machine_type, machine_id"
            ),
        }

    def overview(self) -> list[dict]:
        return self._query(
            """SELECT v.part_id, v.part_no, v.name, p.unit, p.std_cost_twd, v.available, v.reserved,
                 v.inspecting, v.defective, v.on_hand, v.safety_stock,
                 COALESCE((SELECT SUM(qty - qty_shipped) FROM sales_orders o
                           WHERE o.part_id = v.part_id AND o.status != '已出貨'), 0) AS open_demand,
                 COALESCE((SELECT SUM(qty_planned - qty_done) FROM work_orders w
                           WHERE w.part_id = v.part_id AND w.status != '已完工'), 0)
                   AS in_production
               FROM v_part_stock v JOIN parts p ON p.part_id = v.part_id
               ORDER BY v.part_id"""
        )

    def part_inventory(self, part_id: str) -> dict | None:
        head = [r for r in self.overview() if r["part_id"] == part_id]
        if not head:
            return None
        p = self._query(
            "SELECT reorder_qty, lead_time_days, make_or_buy FROM parts WHERE part_id = ?",
            (part_id,),
        )[0]
        return {
            **head[0],
            **p,
            "as_of": self.as_of,
            "locations": self._query(
                """SELECT s.warehouse_id, w.name AS warehouse_name, s.bin, s.lot_no, s.status,
                     s.qty, s.received_on, s.note
                   FROM stock s JOIN warehouses w ON w.warehouse_id = s.warehouse_id
                   WHERE s.part_id = ?
                   ORDER BY CASE s.status WHEN '可用' THEN 0 WHEN '保留' THEN 1 WHEN '待檢' THEN 2
                     ELSE 3 END, s.warehouse_id, s.lot_no""",
                (part_id,),
            ),
            "work_orders": self._query(
                "SELECT wo_no, qty_planned, qty_done, status, line, start_on, due_on, note"
                " FROM work_orders WHERE part_id = ? AND status != '已完工' ORDER BY due_on",
                (part_id,),
            ),
            "sales_orders": self._query(
                "SELECT so_no, line_no, customer, qty, qty_shipped, due_on, status"
                " FROM sales_orders WHERE part_id = ? AND status != '已出貨' ORDER BY due_on",
                (part_id,),
            ),
            "recent_moves": self._query(
                "SELECT moved_on, warehouse_id, move_type, qty, ref_no, note FROM stock_moves"
                " WHERE part_id = ? ORDER BY move_id DESC LIMIT 8",
                (part_id,),
            ),
        }

    def ping(self) -> bool:
        try:
            return bool(self._query("SELECT 1 AS ok"))
        except Exception:  # noqa: BLE001 — 健康檢查只回報可不可用
            return False


_repo: InventoryRepo | None = None


def get_inventory_repo() -> InventoryRepo:
    global _repo
    if _repo is None or _repo.path.parent != get_settings().data_dir:
        _repo = InventoryRepo(get_settings().data_dir / "inventory.sqlite3")
    return _repo
