"""修改資料的白名單操作：每種操作都是寫死的參數化 SQL，模型不寫修改用的 SQL。

作用在「工廠資料庫的 schema」上（inventory.sqlite3 的結構），三個地方共用同一份程式：
1. 試算：把工廠資料庫複製到記憶體，在交易內套用後讀出前後差異，再回滾
2. 指紋：受影響資料列的修改前內容（確認時、主管核准時比對，資料被別人改過就不寫入）
3. 重建：工廠資料庫重建時，依序重播 production.sqlite3 的 data_changes（寫入端只有那一個檔案）

庫存的每次增減都同時寫一筆 stock_moves（盤點調整、調撥出／入、報廢），
所以「各倉異動加總＝目前庫存」在修改後仍然成立。
"""

import hashlib
import json
import sqlite3
from datetime import date

STOCK_STATUS = ("可用", "保留", "待檢", "不良")
# 工單的開立與取消寫在 production.sqlite3 的 work_orders（沿用圖紙頁的流程），不需要重播
REPLAYED = {
    "stock_adjust",
    "stock_transfer",
    "stock_scrap",
    "stock_status",
    "so_update",
    "wo_update_due",
}


class ChangeError(Exception):
    """違反硬性上限（庫存變成負數、訂購數量少於已出貨…）或找不到資料：直接拒絕，主管也不能核准。"""


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def stock_rows(
    conn: sqlite3.Connection, part_id: str, warehouse_id: str, status: str
) -> list[dict]:
    """同一零件、倉庫、狀態的庫存列：扣數量時先扣最新入庫的批（後進先出，保留舊批號的追溯）。"""
    return _rows(
        conn,
        "SELECT stock_id, bin, lot_no, qty, received_on, note FROM stock"
        " WHERE part_id = ? AND warehouse_id = ? AND status = ?"
        " ORDER BY received_on DESC, stock_id DESC",
        (part_id, warehouse_id, status),
    )


def qty_of(conn: sqlite3.Connection, part_id: str, warehouse_id: str, status: str) -> int:
    return sum(r["qty"] for r in stock_rows(conn, part_id, warehouse_id, status))


def _take(conn, part_id: str, warehouse_id: str, status: str, qty: int) -> list[tuple[dict, int]]:
    """扣掉 qty 件，回傳 [(原庫存列, 扣了幾件)]；不夠扣就拒絕（庫存不能變成負數）。"""
    rows = stock_rows(conn, part_id, warehouse_id, status)
    have = sum(r["qty"] for r in rows)
    if qty > have:
        raise ChangeError(
            f"{warehouse_id}「{status}」只有 {have} 件，不能扣 {qty} 件（庫存不能變成負數）"
        )
    taken, left = [], qty
    for r in rows:
        if left <= 0:
            break
        n = min(r["qty"], left)
        if n == r["qty"]:
            conn.execute("DELETE FROM stock WHERE stock_id = ?", (r["stock_id"],))
        else:
            conn.execute("UPDATE stock SET qty = qty - ? WHERE stock_id = ?", (n, r["stock_id"]))
        taken.append((r, n))
        left -= n
    return taken


def _put(
    conn,
    part_id: str,
    warehouse_id: str,
    status: str,
    qty: int,
    *,
    bin_: str,
    lot_no: str,
    received_on: str,
    note: str | None,
) -> None:
    """加 qty 件：同儲位、同批號、同狀態已有一列就加在那一列，否則新增一列。"""
    row = conn.execute(
        "SELECT stock_id FROM stock WHERE part_id = ? AND warehouse_id = ? AND status = ?"
        " AND bin = ? AND lot_no = ?",
        (part_id, warehouse_id, status, bin_, lot_no),
    ).fetchone()
    if row:
        conn.execute("UPDATE stock SET qty = qty + ? WHERE stock_id = ?", (qty, row[0]))
    else:
        conn.execute(
            "INSERT INTO stock (part_id, warehouse_id, bin, lot_no, status, qty, received_on, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (part_id, warehouse_id, bin_, lot_no, status, qty, received_on, note),
        )


def _move(
    conn, on: str, part_id: str, warehouse_id: str, kind: str, qty: int, ref: str, note: str | None
):
    conn.execute(
        "INSERT INTO stock_moves (moved_on, part_id, warehouse_id, move_type, qty, ref_no, note)"
        " VALUES (?,?,?,?,?,?,?)",
        (on, part_id, warehouse_id, kind, qty, ref, note),
    )


def _bin_for(conn, part_id: str, warehouse_id: str) -> str:
    """目的倉沒有這個零件時放到「待上架」儲位。"""
    row = conn.execute(
        "SELECT bin FROM stock WHERE part_id = ? AND warehouse_id = ? ORDER BY stock_id LIMIT 1",
        (part_id, warehouse_id),
    ).fetchone()
    return row[0] if row else f"{warehouse_id.removeprefix('WH-')}-待上架"


def apply(conn: sqlite3.Connection, op: str, p: dict, ref_no: str, on: str | None = None) -> int:
    """套用一筆修改（呼叫端負責交易：試算時回滾、重建時提交）。p 是已正規化的參數。

    回傳動到的資料列數（庫存、訂單、工單；不含異動紀錄），給「單次影響筆數」的硬性上限用。
    """
    on = on or date.today().isoformat()
    before = conn.total_changes
    moves = _count_moves(conn)
    note = p.get("note")
    if op == "stock_adjust":
        delta = int(p["delta"])
        status = p.get("status", "可用")
        if delta < 0:
            _take(conn, p["part_id"], p["warehouse_id"], status, -delta)
        elif delta > 0:
            rows = stock_rows(conn, p["part_id"], p["warehouse_id"], status)
            base = rows[0] if rows else None
            _put(
                conn,
                p["part_id"],
                p["warehouse_id"],
                status,
                delta,
                bin_=base["bin"] if base else _bin_for(conn, p["part_id"], p["warehouse_id"]),
                lot_no=base["lot_no"] if base else ref_no,
                received_on=base["received_on"] if base else on,
                note=base["note"] if base else note,
            )
        _move(
            conn, on, p["part_id"], p["warehouse_id"], "盤點調整", delta, ref_no, note or "盤點調整"
        )
    elif op == "stock_transfer":
        qty, status = int(p["qty"]), p.get("status", "可用")
        dest_bin = _bin_for(conn, p["part_id"], p["to_warehouse_id"])
        for row, n in _take(conn, p["part_id"], p["from_warehouse_id"], status, qty):
            _put(
                conn,
                p["part_id"],
                p["to_warehouse_id"],
                status,
                n,
                bin_=dest_bin,
                lot_no=row["lot_no"],
                received_on=row["received_on"],
                note=row["note"],
            )
        _move(conn, on, p["part_id"], p["from_warehouse_id"], "調撥出", -qty, ref_no, note)
        _move(conn, on, p["part_id"], p["to_warehouse_id"], "調撥入", qty, ref_no, note)
    elif op == "stock_scrap":
        qty = int(p["qty"])
        _take(conn, p["part_id"], p["warehouse_id"], p.get("status", "可用"), qty)
        _move(conn, on, p["part_id"], p["warehouse_id"], "報廢", -qty, ref_no, note or "報廢")
    elif op == "stock_status":
        qty = int(p["qty"])
        if p["from_status"] == p["to_status"]:
            raise ChangeError(f"狀態已經是「{p['to_status']}」")
        for row, n in _take(conn, p["part_id"], p["warehouse_id"], p["from_status"], qty):
            _put(
                conn,
                p["part_id"],
                p["warehouse_id"],
                p["to_status"],
                n,
                bin_=row["bin"],
                lot_no=row["lot_no"],
                received_on=row["received_on"],
                note=note,
            )
    elif op == "so_update":
        row = _so(conn, p["so_no"], p["line_no"])
        qty = int(p.get("qty") or row["qty"])
        if qty < row["qty_shipped"]:
            raise ChangeError(f"訂購數量 {qty} 少於已出貨 {row['qty_shipped']} 件")
        status = (
            "已出貨"
            if qty == row["qty_shipped"]
            else ("部分出貨" if row["qty_shipped"] else "待出貨")
        )
        conn.execute(
            "UPDATE sales_orders SET qty = ?, due_on = ?, status = ?"
            " WHERE so_no = ? AND line_no = ?",
            (qty, p.get("due_on") or row["due_on"], status, p["so_no"], p["line_no"]),
        )
    elif op == "wo_update_due":
        _wo(conn, p["wo_no"])
        conn.execute("UPDATE work_orders SET due_on = ? WHERE wo_no = ?", (p["due_on"], p["wo_no"]))
    elif op == "wo_create":  # 只在試算時用：真正開立走 production_repo（沿用圖紙頁流程）
        conn.execute(
            "INSERT INTO work_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                p["wo_no"],
                p["part_id"],
                int(p["qty"]),
                0,
                "已開立",
                p.get("priority", "一般"),
                "待排程",
                p.get("release_on") or on,
                p["due_on"],
                note,
            ),
        )
    elif op == "wo_cancel":  # 只在試算時用
        _wo(conn, p["wo_no"])
        conn.execute("UPDATE work_orders SET status = '已取消' WHERE wo_no = ?", (p["wo_no"],))
    else:
        raise ChangeError(f"不在白名單的操作：{op}")
    return conn.total_changes - before - (_count_moves(conn) - moves)


def _count_moves(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM stock_moves").fetchone()[0]


def _so(conn, so_no: str, line_no: int) -> dict:
    rows = _rows(
        conn, "SELECT * FROM sales_orders WHERE so_no = ? AND line_no = ?", (so_no, line_no)
    )
    if not rows:
        raise ChangeError(f"找不到訂單 {so_no} 第 {line_no} 項")
    return rows[0]


def _wo(conn, wo_no: str) -> dict:
    rows = _rows(conn, "SELECT * FROM work_orders WHERE wo_no = ?", (wo_no,))
    if not rows:
        raise ChangeError(f"找不到工單 {wo_no}")
    return rows[0]


def snapshot(conn: sqlite3.Connection, op: str, p: dict) -> list[dict]:
    """受影響的資料（給前後差異與指紋）：[{key, label, fields: {欄位: 值}}]。"""
    out: list[dict] = []
    if op.startswith("stock_"):
        whs = (
            [p.get("warehouse_id")]
            if op != "stock_transfer"
            else [p.get("from_warehouse_id"), p.get("to_warehouse_id")]
        )
        names = dict(conn.execute("SELECT warehouse_id, name FROM warehouses").fetchall())
        for wh in whs:
            if not wh:
                continue
            qty = {s: qty_of(conn, p["part_id"], wh, s) for s in STOCK_STATUS}
            out.append(
                {
                    "key": f"stock:{p['part_id']}:{wh}",
                    "label": f"{wh} {names.get(wh, '')}",
                    "fields": qty,
                }
            )
    elif op == "so_update":
        rows = _rows(
            conn,
            "SELECT * FROM sales_orders WHERE so_no = ? AND line_no = ?",
            (p.get("so_no"), p.get("line_no")),
        )
        for r in rows:
            out.append(
                {
                    "key": f"so:{r['so_no']}:{r['line_no']}",
                    "label": f"{r['so_no']} 第 {r['line_no']} 項（{r['customer']}）",
                    "fields": {
                        "訂購數量": r["qty"],
                        "已出貨": r["qty_shipped"],
                        "交期": r["due_on"],
                        "狀態": r["status"],
                    },
                }
            )
    elif op.startswith("wo_"):
        rows = _rows(conn, "SELECT * FROM work_orders WHERE wo_no = ?", (p.get("wo_no"),))
        for r in rows:
            out.append(
                {
                    "key": f"wo:{r['wo_no']}",
                    "label": r["wo_no"],
                    "fields": {
                        "數量": r["qty_planned"] - r["qty_done"],
                        "優先": r["priority"],
                        "交期": r["due_on"],
                        "狀態": r["status"],
                    },
                }
            )
        if op == "wo_create" and not rows:
            out.append(
                {
                    "key": f"wo:{p.get('wo_no')}",
                    "label": p.get("wo_no") or "新工單",
                    "fields": {"數量": None, "優先": None, "交期": None, "狀態": None},
                }
            )
    return out


def fingerprint(conn: sqlite3.Connection, op: str, p: dict) -> str:
    """受影響資料列的修改前內容：確認時、主管核准時再算一次，不同就代表資料被別人改過。"""
    if op == "wo_create":  # 新工單沒有「修改前」的資料，單號在寫入時才配
        return hashlib.sha256(f"wo_create:{p.get('part_id')}".encode()).hexdigest()[:16]
    snap = snapshot(conn, op, p)
    if op.startswith("stock_"):  # 庫存要連批號、儲位一起比
        whs = {p.get("warehouse_id"), p.get("from_warehouse_id"), p.get("to_warehouse_id")} - {None}
        snap.append(
            {
                "rows": [
                    _rows(
                        conn,
                        "SELECT bin, lot_no, status, qty FROM stock WHERE part_id = ?"
                        " AND warehouse_id = ? ORDER BY stock_id",
                        (p["part_id"], wh),
                    )
                    for wh in sorted(whs)
                ]
            }
        )
    raw = json.dumps(snap, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def diff(before: list[dict], after: list[dict]) -> list[dict]:
    """前後差異：[{label, field, before, after}]，只列有變動的欄位。"""
    after_by = {s["key"]: s for s in after}
    out = []
    for b in before:
        a = after_by.get(b["key"], {"fields": {}})
        for f, v in b["fields"].items():
            nv = a["fields"].get(f)
            if nv != v:
                out.append({"label": b["label"], "field": f, "before": v, "after": nv})
    keys = {b["key"] for b in before}
    for a in after:
        if a["key"] not in keys:
            for f, v in a["fields"].items():
                out.append({"label": a["label"], "field": f, "before": None, "after": v})
    return out
