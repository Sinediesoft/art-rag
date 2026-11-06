"""庫存資料庫（Text-to-SQL 的查詢對象）：PostgreSQL 的 inventory schema（docs/adr/048、004）。

資料來源是 kb/inventory/ 的 JSON（一張圖紙一個檔）、kb/parts/ 的圖紙主檔、kb/production/ 的機台，
再加上 production schema 裡使用者開立的工單與目前排程（生產排程，見 production_repo.py）；
任一來源有變動時自動重建（整個 inventory schema 在一個交易裡砍掉重建，
查詢中的連線看到的是舊的或新的，不會看到一半）。SQL 只寫在這層。

模型產生的 SQL 由 run_readonly() 用 **只有 SELECT 權限的資料庫角色** 執行
（取代 SQLite 的 authorizer）：
1. 每個機密等級組合一個唯讀登入角色＋一個專屬 schema，裡面只有檢視表
   （零件依機密等級過濾，庫存、異動、工單、訂單、排程跟著零件過濾）；
   角色只拿得到自己 schema 的 USAGE 與這些檢視表的 SELECT，
   inventory、production、factory 與其他資料表一律 permission denied
2. 唯讀交易（default_transaction_read_only）＋ statement_timeout，加上回傳列數上限
3. 呼叫前已通過 text2sql.check_sql 的靜態檢查（只准一條 SELECT、不准系統目錄與設定函式）
唯讀角色的密碼由系統產生、存在 factory schema（只有主帳號讀得到）。
"""

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row, tuple_row

from app.core.config import REPO_ROOT, get_access_config
from app.core.logging import log
from app.repositories import factory_db

# 改了資料表定義就遞增，已建好的資料庫會自動重建
SCHEMA_VERSION = 3

# (資料表, 說明, [(欄位, 型別與限制, 說明)])
# 建表 DDL、給模型的 schema、/inventory/schema 都從這裡產生。
# REFERENCES 只寫給模型看（關聯怎麼 JOIN）：建表時拿掉，和 SQLite 版一樣不強制外鍵，
# 一致性由知識庫驗證負責
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
            ("std_cost_twd", "NUMERIC", "標準成本（元／件）"),
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
# 沒有給值就自動編號的欄位（SQLite 版是 INTEGER PRIMARY KEY 的 rowid）
IDENTITY = {"stock": "stock_id", "stock_moves": "move_id"}
# 哪些資料表跟著零件的機密等級過濾（唯讀角色的檢視表）
_BY_PART = ("stock", "stock_moves", "work_orders", "sales_orders")

# 檢視表：非彙總欄位全部列進 GROUP BY（唯讀角色查的是檢視表，PostgreSQL 不會從檢視表推得主鍵）
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
GROUP BY p.part_id, p.part_no, p.name, p.safety_stock""",
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
GROUP BY w.wo_no, w.part_id, p.name, w.priority, w.qty_planned, w.qty_done, w.due_on""",
    ),
]

ALLOWED_TABLES = frozenset([t[0] for t in TABLES] + [v[0] for v in VIEWS])
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
    """inventory schema 的建表語法（執行時 search_path 已設成 inventory）。"""
    parts = []
    for name, _, cols in TABLES:
        lines = []
        for c, decl, _ in cols:
            decl = decl.split(" REFERENCES ")[0]
            if IDENTITY.get(name) == c:
                decl = "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
            lines.append(f"  {c} {decl}")
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
    """知識庫 JSON＋production schema 的資料版本：任一變動就重建。"""
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


class Db:
    """data_changes 用的薄包裝：查詢回 dict 列，寫入累計影響列數
    （取代 SQLite 的 total_changes）。"""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self.changes = 0

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> tuple | None:
        with self.conn.cursor(row_factory=tuple_row) as cur:
            return cur.execute(sql, params).fetchone()

    def exec(self, sql: str, params: tuple = (), count: bool = True) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            n = max(cur.rowcount, 0)
        if count:
            self.changes += n
        return n

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        with self.conn.transaction():
            yield


def _fill(db: Db, site: dict, parts: list[dict], items: list[dict], production: dict) -> None:
    """把知識庫與 production 的資料寫進剛建好的 inventory schema（search_path 已是 inventory）。"""
    cur = db.conn.cursor()
    inv = {it["part_id"]: it for it in items}
    cur.executemany(
        "INSERT INTO parts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (p["id"], p["part_no"], p["drawing_no"], p["revision"], p["name"]["zh"],
             p["category"], p["material"], p["confidentiality"], inv.get(p["id"], {}).get("unit"),
             inv.get(p["id"], {}).get("std_cost_twd"), inv.get(p["id"], {}).get("safety_stock"),
             inv.get(p["id"], {}).get("reorder_qty"), inv.get(p["id"], {}).get("lead_time_days"),
             inv.get(p["id"], {}).get("make_or_buy"))
            for p in parts
        ],
    )  # fmt: skip
    cur.executemany(
        "INSERT INTO warehouses VALUES (%s,%s,%s,%s)",
        [(w["warehouse_id"], w["name"], w["kind"], w["site"]) for w in site["warehouses"]],
    )
    moves = []
    for it in items:
        pid = it["part_id"]
        cur.executemany(
            "INSERT INTO stock (part_id, warehouse_id, bin, lot_no, status, qty, received_on,"
            " note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (pid, s["warehouse_id"], s["bin"], s["lot_no"], s["status"], s["qty"],
                 s["received_on"], s.get("note"))
                for s in it["stock"]
            ],
        )  # fmt: skip
        moves += [(m["moved_on"], pid, i, m) for i, m in enumerate(it["moves"])]
        cur.executemany(
            "INSERT INTO work_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (w["wo_no"], pid, w["qty_planned"], w["qty_done"], w["status"], "一般",
                 w["line"], w["start_on"], w["due_on"], w.get("note"))
                for w in it["work_orders"]
            ],
        )  # fmt: skip
        cur.executemany(
            "INSERT INTO sales_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (s["so_no"], s["line_no"], s["customer"], pid, s["qty"], s["qty_shipped"],
                 s["order_on"], s["due_on"], s["status"])
                for s in it["sales_orders"]
            ],
        )  # fmt: skip
    # 異動依日期排序編號，move_id 越大越新
    cur.executemany(
        "INSERT INTO stock_moves (moved_on, part_id, warehouse_id, move_type, qty, ref_no,"
        " note) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        [
            (m["moved_on"], pid, m["warehouse_id"], m["move_type"], m["qty"], m.get("ref_no"),
             m.get("note"))
            for _, pid, _, m in sorted(moves, key=lambda x: x[:3])
        ],
    )  # fmt: skip
    # 生產排程：機台、使用者開立的工單、目前排程（工單已不存在的排程列略過）
    cur.executemany(
        "INSERT INTO machines VALUES (%s,%s,%s,%s)",
        [
            (m["machine_id"], m["name"], m["machine_type"], m["site"])
            for m in production.get("machines", [])
        ],
    )
    part_ids = {p["id"] for p in parts}
    cur.executemany(
        "INSERT INTO work_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        [r for r in _user_work_orders(production) if r[1] in part_ids],
    )
    known = {r["wo_no"] for r in db.rows("SELECT wo_no FROM work_orders")}
    _replay_changes(db, production.get("changes", []))
    cur.executemany(
        "INSERT INTO schedule_ops VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        [
            (o["wo_no"], o["op_seq"], o["op_name"], o["kind"], o["machine_id"], o["start_at"],
             o["end_at"], o["setup_min"], o["run_min"])
            for o in production.get("ops", [])
            if o["wo_no"] in known
        ],
    )  # fmt: skip
    cur.close()


def _replay_changes(db: Db, changes: list[dict]) -> None:
    """智慧助理寫入的異動（production.data_changes）依序重播；每筆各自一個 savepoint，
    某筆因知識庫改了而不再成立（例如庫存不夠扣）就跳過並記錄，不影響其他筆。"""
    from app.repositories.data_changes import REPLAYED, ChangeError, apply

    for ch in changes:
        if ch["op"] not in REPLAYED:
            continue
        try:
            with db.savepoint():
                apply(db, ch["op"], ch["params"], ch["change_no"], ch["moved_on"])
        except (ChangeError, psycopg.Error) as e:
            if isinstance(e, psycopg.OperationalError):
                raise  # 資料庫連不上：整個重建失敗，不是這筆異動的問題
            log.error(f"異動 {ch['change_no']} 無法重播，已略過：{e}")


# ------------------------------------------------------------ Text-to-SQL 的唯讀角色
_META_DDL = """
CREATE SCHEMA IF NOT EXISTS factory;
REVOKE ALL ON SCHEMA factory FROM PUBLIC;
CREATE TABLE IF NOT EXISTS factory.inventory_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), seed_hash TEXT NOT NULL, manifest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factory.sql_readers (
  key TEXT PRIMARY KEY, role TEXT NOT NULL, schema TEXT NOT NULL, levels TEXT,
  password TEXT NOT NULL
);
"""


def _levels_key(levels: tuple[str, ...] | None) -> str:
    """機密等級組合的代號：None＝不限（看得到全部等級）。"""
    if levels is None:
        return "all"
    return hashlib.sha256("|".join(sorted(set(levels))).encode()).hexdigest()[:10]


def _level_sets() -> list[tuple[str, ...] | None]:
    """建庫時先準備好的組合：不限，加上 access.yaml 各角色（看不到全部等級的）的組合。"""
    cfg = get_access_config()
    all_levels = set(cfg.get("levels", ["公開", "內部", "機密"]))
    out: list[tuple[str, ...] | None] = [None]
    for c in (cfg.get("clearance") or {}).values():
        lv = tuple(c.get("levels") or ())
        if lv and not all_levels <= set(lv) and _levels_key(lv) not in map(_levels_key, out):
            out.append(lv)
    return out


def _reader_views(conn: psycopg.Connection, schema: str, levels: tuple[str, ...] | None) -> None:
    """唯讀角色的檢視表：和 inventory 同名，零件依機密等級過濾，其他資料跟著零件過濾
    （和 SQLite 版的「刪掉看不到的零件與相關資料的記憶體複本」結果相同）。"""
    s = pgsql.Identifier(schema)
    conn.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s))
    conn.execute(pgsql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(s))
    conn.execute(pgsql.SQL("SET LOCAL search_path TO {}").format(s))
    barrier = pgsql.SQL(" WITH (security_barrier)")
    for name, _, _ in TABLES:
        t = pgsql.Identifier(name)
        if levels is None or name in ("warehouses", "machines"):
            q = pgsql.SQL("CREATE VIEW {} AS SELECT * FROM inventory.{}").format(t, t)
        elif name == "parts":
            q = pgsql.SQL(
                "CREATE VIEW parts{} AS SELECT * FROM inventory.parts"
                " WHERE confidentiality = ANY({})"
            ).format(barrier, pgsql.Literal(list(levels)))
        elif name in _BY_PART:
            q = pgsql.SQL(
                "CREATE VIEW {}{} AS SELECT t.* FROM inventory.{} t"
                " WHERE t.part_id IN (SELECT part_id FROM parts)"
            ).format(t, barrier, t)
        else:  # schedule_ops：跟著工單
            q = pgsql.SQL(
                "CREATE VIEW schedule_ops{} AS SELECT t.* FROM inventory.schedule_ops t"
                " WHERE t.wo_no IN (SELECT wo_no FROM work_orders)"
            ).format(barrier)
        conn.execute(q)
    for name, _, _, body in VIEWS:  # 彙總檢視表建在過濾後的檢視表上
        conn.execute(
            pgsql.SQL("CREATE VIEW {} AS ").format(pgsql.Identifier(name)) + pgsql.SQL(body)
        )
    conn.execute("SET LOCAL search_path TO inventory")


def _ensure_reader(conn: psycopg.Connection, levels: tuple[str, ...] | None) -> dict:
    """建（或補）一個機密等級組合的唯讀角色與它的檢視表；呼叫端持有 INVENTORY_BUILD 鎖。"""
    key = _levels_key(levels)
    db_name = conn.execute("SELECT current_database() AS d").fetchone()["d"]
    role = f"artrag_sql_{factory_db.ident_part(db_name)}_{key}"
    schema = f"inventory_ro_{key}"
    row = conn.execute("SELECT * FROM factory.sql_readers WHERE key = %s", (key,)).fetchone()
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    r = pgsql.Identifier(role)
    if row is None or not exists:
        password = secrets.token_urlsafe(24)
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            pgsql.SQL(
                verb + " ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                " NOINHERIT NOREPLICATION CONNECTION LIMIT 20"
            ).format(r, pgsql.Literal(password))
        )
        conn.execute(
            "INSERT INTO factory.sql_readers VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key)"
            " DO UPDATE SET role = EXCLUDED.role, schema = EXCLUDED.schema,"
            " levels = EXCLUDED.levels, password = EXCLUDED.password",
            (key, role, schema, json.dumps(levels, ensure_ascii=False), password),
        )
        row = {"role": role, "schema": schema, "password": password}
    # 角色層級的預設值：就算連線參數被改掉，登入後還是唯讀、只看得到自己的 schema
    conn.execute(
        pgsql.SQL("ALTER ROLE {} SET search_path TO {}").format(r, pgsql.Identifier(schema))
    )
    conn.execute(pgsql.SQL("ALTER ROLE {} SET default_transaction_read_only TO on").format(r))
    has_views = conn.execute(
        "SELECT 1 FROM pg_views WHERE schemaname = %s AND viewname = 'parts'", (schema,)
    ).fetchone()
    if not has_views:
        _reader_views(conn, schema, levels)
    s = pgsql.Identifier(schema)
    conn.execute(pgsql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
    conn.execute(pgsql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, r))
    return {"role": row["role"], "schema": row["schema"], "password": row["password"]}


_meta_ready: set[str] = set()


class InventoryRepo:
    def __init__(self):
        self._lock = threading.Lock()
        self._checked_hash = ""
        self._checked_at = 0.0
        self._readers: dict[str, dict] = {}
        self.manifest: dict = {}
        self.problems: list[str] = []

    # ------------------------------------------------------------ 建庫
    def _state(self, conn: psycopg.Connection) -> dict | None:
        u = factory_db.url()
        if u not in _meta_ready:  # 每個行程每個資料庫建一次；拿鎖避免兩個行程同時 CREATE 撞名
            factory_db.xact_lock(conn, factory_db.INVENTORY_BUILD)
            conn.execute(_META_DDL)
            _meta_ready.add(u)
        row = conn.execute("SELECT * FROM factory.inventory_state WHERE id = 1").fetchone()
        return (
            {"seed_hash": row["seed_hash"], "manifest": json.loads(row["manifest"])}
            if row
            else None
        )

    def ready(self) -> bool:
        """庫存資料庫建好了沒有（建庫時知識庫有誤、又沒有舊資料可沿用時為 False）。"""
        return bool(self.manifest)

    def ensure_built(self, force: bool = False) -> None:
        """kb/inventory、kb/parts、kb/production 或 production schema 有變動就重建
        （make demo-add、開立工單、排程完成後不必重啟後端）。

        每秒最多檢查一次，避免每個查詢都重算雜湊；force=True 立刻檢查（寫入後呼叫）。
        """
        now = time.monotonic()
        if not force and now - self._checked_at < 1.0 and self.manifest:
            return
        self._checked_at = now
        current = seed_hash()
        if current == self._checked_hash and self.manifest:
            return
        with self._lock:
            if current == self._checked_hash and self.manifest:
                return
            with factory_db.connection() as conn:
                state = self._state(conn)
            if state and state["seed_hash"] == current:
                self.manifest = state["manifest"]
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
            if not self.manifest:
                with factory_db.connection() as conn:
                    state = self._state(conn)
                self.manifest = state["manifest"] if state else {}
            return self.manifest
        production = get_production_repo().snapshot()
        production["machines"] = prod_site["machines"] if prod_site else []
        with factory_db.connection() as conn:
            # 同一時間只有一個行程重建；拿到鎖時別的行程可能已經建好同一份，就不再重建
            factory_db.xact_lock(conn, factory_db.INVENTORY_BUILD)
            state = self._state(conn)
            if state and state["seed_hash"] == current:
                self.manifest = state["manifest"]
                return self.manifest
            conn.execute("DROP SCHEMA IF EXISTS inventory CASCADE")  # 唯讀角色的檢視表一起砍掉
            conn.execute("CREATE SCHEMA inventory")
            conn.execute("REVOKE ALL ON SCHEMA inventory FROM PUBLIC")
            conn.execute("SET LOCAL search_path TO inventory")
            conn.execute(_ddl())
            db = Db(conn)
            _fill(db, site, parts, items, production)
            counts = {
                t: db.one(pgsql.SQL("SELECT COUNT(*) FROM {}").format(pgsql.Identifier(t)))[0]
                for t, _, _ in TABLES
            }
            readers = {}
            for levels in _level_sets():
                readers[_levels_key(levels)] = _ensure_reader(conn, levels)
            manifest = {
                "seed_hash": current,
                "schema_version": SCHEMA_VERSION,
                "as_of": site["as_of"],
                "company": site["company"],
                "tables": counts,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            conn.execute(
                "INSERT INTO factory.inventory_state VALUES (1, %s, %s) ON CONFLICT (id)"
                " DO UPDATE SET seed_hash = EXCLUDED.seed_hash, manifest = EXCLUDED.manifest",
                (current, json.dumps(manifest, ensure_ascii=False)),
            )
        self.manifest = manifest
        self._readers = readers
        log.info(f"庫存資料庫已重建：{counts}")
        return self.manifest

    @property
    def as_of(self) -> str:
        return self.manifest.get("as_of", "")

    # ------------------------------------------------------------ 連線
    @contextmanager
    def _owner(self) -> Iterator[psycopg.Connection]:
        """主帳號、search_path＝inventory 的連線（系統自己的固定查詢與試算，不經模型）。"""
        self.ensure_built()
        with factory_db.connection() as conn:
            conn.execute("SET LOCAL search_path TO inventory")
            yield conn

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """系統自己的固定查詢（非模型產生）。"""
        with self._owner() as conn:
            return conn.execute(sql, params).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """系統自己的固定查詢（智慧助理推定倉庫、讀回寫入結果）；不經模型，參數用 %s。"""
        return self._query(sql, params)

    @contextmanager
    def scratch(self) -> Iterator[Db]:
        """修改資料的試算、讀回：在一個一定回滾的交易裡操作 inventory schema
        （取代 SQLite 的記憶體複本）。
        區塊結束（不論成功與否）都回滾，什麼都不會留下。"""
        with self._owner() as conn:
            try:
                yield Db(conn)
            finally:
                conn.rollback()

    def _reader(self, levels: tuple[str, ...] | None) -> dict:
        key = _levels_key(levels)
        if key in self._readers:
            return self._readers[key]
        with factory_db.connection() as conn:
            row = conn.execute(
                "SELECT role, schema, password FROM factory.sql_readers WHERE key = %s", (key,)
            ).fetchone()
            has_views = (
                row
                and conn.execute(
                    "SELECT 1 FROM pg_views WHERE schemaname = %s AND viewname = 'parts'",
                    (row["schema"],),
                ).fetchone()
            )
            if not has_views:  # access.yaml 之後才加的等級組合：現在補建
                factory_db.xact_lock(conn, factory_db.INVENTORY_BUILD)
                conn.execute("SET LOCAL search_path TO inventory")
                row = _ensure_reader(conn, levels)
        self._readers[key] = dict(row)
        return self._readers[key]

    def run_readonly(
        self, sql: str, max_rows: int, timeout_ms: int, levels: tuple[str, ...] | None = None
    ) -> QueryResult:
        """用唯讀角色執行模型產生的 SQL（呼叫前已通過 text2sql.check_sql 的靜態檢查）。
        levels：這個身分看得到的機密等級；None＝不限（看得到全部等級、或程式內部呼叫）。"""
        self.ensure_built()
        r = self._reader(levels)
        options = {
            "search_path": r["schema"],
            "default_transaction_read_only": "on",
            "statement_timeout": str(int(timeout_ms)),
            "idle_in_transaction_session_timeout": str(int(timeout_ms) + 5000),
        }
        t0 = time.perf_counter()
        try:
            conn = factory_db.connect_as(r["role"], r["password"], options)
        except psycopg.OperationalError as e:
            if "password authentication failed" in str(e):  # 別的行程重設了密碼：重讀一次
                self._readers.pop(_levels_key(levels), None)
                r = self._reader(levels)
                conn = factory_db.connect_as(r["role"], r["password"], options)
            else:
                raise
        try:
            cur = conn.execute(sql)  # 不帶參數：SQL 裡的 % 照字面傳給資料庫
            rows = cur.fetchmany(max_rows + 1)
            columns = [d.name for d in cur.description or []]
        except psycopg.errors.QueryCanceled as e:
            raise SqlError(f"執行超過 {timeout_ms} ms 被中止，請簡化查詢") from e
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as e:
            raise SqlError("not authorized：只能讀取 schema 中列出的資料表，且只能用 SELECT") from e
        except psycopg.OperationalError:
            raise  # 資料庫連不上：503，不是模型的錯
        except psycopg.Error as e:
            diag = e.diag
            msg = diag.message_primary or str(e)
            if diag.message_hint:
                msg += f"（提示：{diag.message_hint}）"
            raise SqlError(msg) from e
        finally:
            with suppress(psycopg.Error):
                conn.rollback()
            conn.close()
        return QueryResult(
            columns=columns,
            rows=[list(x) for x in rows[:max_rows]],
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
            "SELECT reorder_qty, lead_time_days, make_or_buy FROM parts WHERE part_id = %s",
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
                   WHERE s.part_id = %s
                   ORDER BY CASE s.status WHEN '可用' THEN 0 WHEN '保留' THEN 1 WHEN '待檢' THEN 2
                     ELSE 3 END, s.warehouse_id, s.lot_no""",
                (part_id,),
            ),
            "work_orders": self._query(
                "SELECT wo_no, qty_planned, qty_done, status, line, start_on, due_on, note"
                " FROM work_orders WHERE part_id = %s AND status != '已完工' ORDER BY due_on",
                (part_id,),
            ),
            "sales_orders": self._query(
                "SELECT so_no, line_no, customer, qty, qty_shipped, due_on, status"
                " FROM sales_orders WHERE part_id = %s AND status != '已出貨' ORDER BY due_on",
                (part_id,),
            ),
            "recent_moves": self._query(
                "SELECT moved_on, warehouse_id, move_type, qty, ref_no, note FROM stock_moves"
                " WHERE part_id = %s ORDER BY move_id DESC LIMIT 8",
                (part_id,),
            ),
        }

    def ping(self) -> bool:
        try:
            return bool(self._query("SELECT 1 AS ok"))
        except Exception:  # noqa: BLE001 — 健康檢查只回報可不可用
            return False


_repo = InventoryRepo()


def get_inventory_repo() -> InventoryRepo:
    return _repo


def reset_cache() -> None:
    """測試清掉資料庫後呼叫：下次查詢重新檢查要不要建庫、重建中繼資料表。"""
    global _repo
    _repo = InventoryRepo()
    _meta_ready.clear()
