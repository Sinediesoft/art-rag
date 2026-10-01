"""修改資料流程（docs/adr/007）：權限判定 → 試算 → 額度判斷 → 確認寫入／送主管核准 → 讀回回覆。

1. 參數抽取：規則（正規表示式＋名稱對照）→ 資料庫推定 → 缺的欄位才請本地 Qwen3-VL 輸出 JSON
2. 權限判定（預設不允許，依序檢查，任一項不符就拒絕並記錄）：
   角色能不能做這種操作 → 資料在不在他的範圍 → 欄位開不開放 → 硬性上限（主管也不能核准）
3. 試算：工廠資料庫複製到記憶體，在交易內套用、讀出修改前後差異，再回滾
4. 額度判斷：超過就送主管核准（待核准單 AP-）；額度內出確認卡
5. 確認寫入：寫入前再驗一次權限與資料指紋（期間被別人改過、權限被收回都拒絕）→ 異動單＋稽核紀錄
   → 工廠資料庫依版本計數自動重建（Text-to-SQL 立刻查得到）
6. 回覆從資料庫讀回、套固定模板，不讓模型自由描述（不會再出現「已全部設為 0」這種假裝改好的回答）

身分只看伺服器端的工作階段（services/identity.py）；模型只負責理解句子，決定不了權限。
"""

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.agent.entities import get_index
from app.agent.extract import FIELD_LABELS, Extraction, fill_with_llm, infer, missing, rules
from app.agent.local_router import classify_op
from app.core.config import get_access_config, get_agent_config
from app.core.errors import AppError
from app.core.logging import log
from app.repositories import data_changes as dc
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.production_repo import get_production_repo
from app.services import schedule_service
from app.services.identity import Account, accounts, get_account

CHECK_LABELS = {"role": "角色", "scope": "資料範圍", "field": "欄位", "limit": "上限"}
STOCK_OPS = ("stock_adjust", "stock_transfer", "stock_scrap", "stock_status")


def op_label(op: str) -> str:
    return get_agent_config()["modify_ops"].get(op, {}).get("label", op)


@dataclass
class Pending:
    """確認卡：試算結果先存在後端，按確認時用 id 取回（前端送不了也改不了參數）。"""

    id: str
    op: str
    params: dict
    account_id: str
    fingerprint: str
    summary: str
    diff: list[dict]
    reasons: list[str]
    request_id: str
    created: float = field(default_factory=time.monotonic)


_pending: dict[str, Pending] = {}
_write_lock = threading.Lock()  # 寫入與核准一次只做一筆：再驗指紋到寫入之間不會被插隊


def _purge_pending() -> None:
    ttl = float(get_access_config()["approval"]["pending_ttl_min"]) * 60
    now = time.monotonic()
    for k in [k for k, p in _pending.items() if now - p.created > ttl]:
        _pending.pop(k, None)


# ---------------------------------------------------------------- 名稱與摘要
def _names() -> dict:
    hints = get_inventory_repo().value_hints()
    return {
        "parts": {p["part_id"]: p["name"] for p in hints["parts"]},
        "warehouses": {w["warehouse_id"]: w["name"] for w in hints["warehouses"]},
    }


def labels(op: str, p: dict) -> dict:
    """確認卡上的參數（中文欄位名＋顯示值）。"""
    n = _names()
    out = {}
    for k, v in p.items():
        if k in ("note", "qty_delta") or v is None:
            continue
        if k == "part_id":
            v = "全部零件" if v == "*" else f"〈{n['parts'].get(v, v)}〉{v}"
        elif k.endswith("warehouse_id"):
            v = f"{v} {n['warehouses'].get(v, '')}".strip()
        elif k == "delta":
            v = f"{v:+d} 件"
        elif k in ("qty", "counted"):
            v = f"{v} 件"
        out[FIELD_LABELS.get(k, k)] = v
    return out


def summary(op: str, p: dict) -> str:
    n = _names()
    part = n["parts"].get(p.get("part_id", ""), p.get("part_id", ""))
    wh = n["warehouses"].get(p.get("warehouse_id", ""), p.get("warehouse_id", ""))
    if op == "stock_adjust":
        return f"{wh}〈{part}〉{p.get('status', '可用')} {p['delta']:+d} 件（盤點調整）"
    if op == "stock_transfer":
        a = n["warehouses"].get(p["from_warehouse_id"], p["from_warehouse_id"])
        b = n["warehouses"].get(p["to_warehouse_id"], p["to_warehouse_id"])
        return f"〈{part}〉{p['qty']} 件 {a} → {b}（調撥）"
    if op == "stock_scrap":
        return f"{wh}〈{part}〉{p.get('status', '可用')}報廢 {p['qty']} 件"
    if op == "stock_status":
        return f"{wh}〈{part}〉{p['qty']} 件 {p['from_status']} → {p['to_status']}"
    if op == "so_update":
        parts = []
        if p.get("due_on"):
            parts.append(f"交期改為 {p['due_on']}")
        if p.get("qty") is not None:
            parts.append(f"訂購數量改為 {p['qty']} 件")
        return f"{p['so_no']} 第 {p['line_no']} 項" + "、".join(parts)
    if op == "wo_create":
        return f"開立工單〈{part}〉{p['qty']} 件，交期 {p['due_on']}（{p.get('priority', '一般')}）"
    if op == "wo_update_due":
        return f"{p['wo_no']} 交期改為 {p['due_on']}"
    if op == "wo_cancel":
        return f"取消工單 {p['wo_no']}"
    return op_label(op)


# ---------------------------------------------------------------- 正規化
def normalize(x: Extraction) -> dict:
    """抽出的參數 → 可以直接套用的參數（實盤數量換成增減量、補訂單項次、預定工單號…）。"""
    repo = get_inventory_repo()
    p = dict(x.params)
    op = x.op
    if op == "stock_adjust" and "counted" in p:
        mem = repo.copy_to_memory()
        try:
            have = dc.qty_of(mem, p["part_id"], p["warehouse_id"], p.get("status", "可用"))
        finally:
            mem.close()
        p["delta"] = int(p.pop("counted")) - have
        x.notes.append(f"帳面 {have} 件 → 實盤 {have + p['delta']} 件")
        if p["delta"] == 0:
            raise dc.ChangeError("實盤數量與帳面相同，不需要調整")
    if op == "so_update":
        rows = repo.query(
            "SELECT * FROM sales_orders WHERE so_no = ? ORDER BY line_no", (p["so_no"],)
        )
        if not rows:
            raise dc.ChangeError(f"找不到訂單 {p['so_no']}")
        line = next((r for r in rows if r["line_no"] == p.get("line_no")), None)
        if line is None:
            raise dc.ChangeError(f"訂單 {p['so_no']} 沒有第 {p.get('line_no')} 項")
        if "qty_delta" in p:
            p["qty"] = line["qty"] + int(p.pop("qty_delta"))
        p["customer"] = line["customer"]
    if op in ("wo_update_due", "wo_cancel"):
        rows = repo.query("SELECT wo_no FROM work_orders WHERE wo_no = ?", (p["wo_no"],))
        if not rows:
            raise dc.ChangeError(f"找不到工單 {p['wo_no']}")
    if op == "wo_create":
        wo_no, start = schedule_service.next_wo_no()
        p["wo_no"] = wo_no
        p.setdefault("priority", "一般")
        if p["due_on"] < start:
            raise dc.ChangeError(f"交期 {p['due_on']} 早於排程起始日 {start}")
    return p


def _so_lines(x: Extraction) -> None:
    """訂單沒說第幾項：有提到零件就用那個零件的項次；否則還沒出完貨的只有一項就用那一項。"""
    if x.op != "so_update" or "so_no" not in x.params or "line_no" in x.params:
        return
    rows = get_inventory_repo().query(
        "SELECT line_no, part_id, status FROM sales_orders WHERE so_no = ? ORDER BY line_no",
        (x.params["so_no"],),
    )
    pid = x.params.get("part_id")
    pick = [r for r in rows if r["part_id"] == pid] if pid else rows
    if len(pick) > 1:
        pick = [r for r in pick if r["status"] != "已出貨"]
    if len(pick) == 1:
        x.params["line_no"] = pick[0]["line_no"]
        x.sources["line_no"] = "推定"
        if len(rows) > 1:
            x.notes.append(
                f"{x.params['so_no']} 有 {len(rows)} 項，只有第 {pick[0]['line_no']} 項還沒出完貨"
            )


# ---------------------------------------------------------------- 權限判定
def _wo_owner(wo_no: str) -> tuple[bool, str | None]:
    """(是不是圖紙頁／智慧助理開立的工單, 開立人)。"""
    w = get_production_repo().get_work_order(wo_no)
    return (w is not None, w.get("created_by") if w else None)


def authorize(account: Account, op: str, p: dict, scope_all: bool = False) -> list[dict]:
    """依序檢查角色 → 資料範圍 → 欄位；第一項不符之後的標為「未檢查」。上限在試算後才判斷。"""
    checks: list[dict] = []

    def add(key: str, ok: bool | None, detail: str) -> None:
        checks.append({"key": key, "label": CHECK_LABELS[key], "ok": ok, "detail": detail})

    roles = get_access_config()["roles"]
    if op == "other":
        add("role", account.role != "guest", f"身分：{account.label}")
        add("field", False, "零件主檔（成本、安全庫存、料號、材料…）與機台資料不開放用對話修改")
        return checks
    if not account.can(op):
        who = "、".join(a.label for a in accounts().values() if a.can(op))
        add("role", False, f"「{account.label}」不能做{op_label(op)}（可以的身分：{who or '無'}）")
        add("scope", None, "未檢查")
        add("field", None, "未檢查")
        return checks
    add("role", True, f"{roles[account.role]['label']}可以做{op_label(op)}")

    ok, detail = True, ""
    if op in STOCK_OPS:
        whs = [
            p.get(k) for k in ("warehouse_id", "from_warehouse_id", "to_warehouse_id") if p.get(k)
        ]
        if scope_all:
            whs = [
                r["warehouse_id"]
                for r in get_inventory_repo().query(
                    "SELECT DISTINCT warehouse_id FROM stock ORDER BY warehouse_id"
                )
            ]
        outside = [w for w in whs if w not in account.warehouses]
        ok = not outside
        mine = "、".join(account.warehouses)
        detail = (
            f"{'、'.join(whs)} 都在範圍內（{mine}）"
            if ok
            else f"{'、'.join(outside)} 不在「{account.label}」的範圍（{mine}）"
        )
    elif op == "so_update":
        customer = p.get("customer", "")
        ok = customer in account.customers
        detail = (
            f"客戶「{customer}」由{account.label}負責"
            if ok
            else f"客戶「{customer}」不是「{account.label}」負責的客戶"
            f"（{'、'.join(account.customers)}）"
        )
    elif op == "wo_cancel":
        mine, owner = _wo_owner(p["wo_no"])
        if not mine:
            ok, detail = False, f"{p['wo_no']} 是 ERP 匯入的既有工單，不能用對話取消"
        elif owner not in (None, account.id):
            ok, detail = (
                False,
                f"{p['wo_no']} 是「{get_account(owner).label}」開的，只能取消自己開的工單",
            )
        else:
            detail = f"{p['wo_no']} 是自己開立的工單"
    else:
        detail = "全廠"
    add("scope", ok, detail)
    if not ok:
        add("field", None, "未檢查")
        return checks
    allowed = get_access_config()["fields"].get(op, [])
    changed = [
        k for k in ("due_on", "qty") if op == "so_update" and p.get(k) is not None
    ] or allowed
    add(
        "field",
        set(changed) <= set(allowed),
        f"修改欄位：{'、'.join(FIELD_LABELS.get(f, f) for f in changed)}",
    )
    return checks


def quota(op: str, p: dict, before: list[dict]) -> list[str]:
    """額度：超過就送主管核准（初值在 shared/access.yaml）。"""
    q = get_access_config()["quotas"].get(op, {})
    reasons = []
    if op == "stock_adjust":
        have = before[0]["fields"].get(p.get("status", "可用"), 0) if before else 0
        d = abs(int(p["delta"]))
        if d > q["max_qty"]:
            reasons.append(f"盤點差異 {d} 件，超過 {q['max_qty']} 件")
        elif have and d / have > q["max_ratio"]:
            reasons.append(f"盤點差異 {d / have:.0%}（{d}/{have}），超過 {q['max_ratio']:.0%}")
    elif op == "stock_scrap" and int(p["qty"]) > q["max_qty"]:
        reasons.append(f"報廢 {p['qty']} 件，超過 {q['max_qty']} 件")
    elif op == "stock_transfer" and int(p["qty"]) > q["max_qty"]:
        reasons.append(f"調撥 {p['qty']} 件，超過 {q['max_qty']} 件")
    elif op == "so_update" and before:
        old = before[0]["fields"]
        if p.get("due_on"):
            delay = (date.fromisoformat(p["due_on"]) - date.fromisoformat(old["交期"])).days
            if delay > q["max_delay_days"]:
                reasons.append(f"交期延後 {delay} 天，超過 {q['max_delay_days']} 天")
        if q.get("qty_decrease") and p.get("qty") is not None and p["qty"] < old["訂購數量"]:
            reasons.append(f"訂購數量減少 {old['訂購數量'] - p['qty']} 件")
    elif op == "wo_create" and q.get("rush") and p.get("priority") == "急件":
        reasons.append("急件工單（會插隊影響其他工單交期）")
    elif op == "wo_cancel" and q.get("in_progress") and before:
        if before[0]["fields"].get("狀態") in ("生產中", "委外處理中"):
            reasons.append("取消已在生產中的工單")
    return reasons


def trial(op: str, p: dict) -> dict:
    """在記憶體副本的交易內套用、讀出前後差異、回滾。ChangeError＝違反硬性上限。"""
    mem = get_inventory_repo().copy_to_memory()
    try:
        before = dc.snapshot(mem, op, p)
        fp = dc.fingerprint(mem, op, p)
        mem.execute("BEGIN")
        try:
            touched = dc.apply(mem, op, p, ref_no="（試算）")
            after = dc.snapshot(mem, op, p)
        except sqlite3.Error as e:
            raise dc.ChangeError(f"試算失敗：{e}") from e
        finally:
            mem.execute("ROLLBACK")
        return {
            "before": before,
            "after": after,
            "fingerprint": fp,
            "touched": touched,
            "diff": dc.diff(before, after),
        }
    finally:
        mem.close()


def _limit(touched: int, error: str | None) -> dict:
    max_rows = int(get_access_config()["hard_limits"]["max_rows"])
    if error:
        return {"key": "limit", "label": "上限", "ok": False, "detail": error}
    ok = touched <= max_rows
    detail = f"影響 {touched} 筆" + ("" if ok else f"，超過單次 {max_rows} 筆的上限")
    return {"key": "limit", "label": "上限", "ok": ok, "detail": detail}


def _audit(
    account_id: str,
    label: str,
    action: str,
    op: str | None,
    summary_: str,
    ref_no: str | None = None,
    detail=None,
    request_id: str | None = None,
) -> None:
    get_production_repo().add_audit(
        {
            "actor_id": account_id,
            "actor_label": label,
            "action": action,
            "op": op,
            "ref_no": ref_no,
            "summary": summary_,
            "detail": detail,
            "request_id": request_id,
        }
    )


# ---------------------------------------------------------------- 1～4：預覽（確認卡）
async def preview(
    account: Account,
    request_id: str,
    question: str | None = None,
    op: str | None = None,
    params: dict | None = None,
) -> dict:
    t0 = time.perf_counter()
    repo = get_inventory_repo()
    repo.ensure_built()
    if question:
        entities = get_index().find(question)
        op = op or classify_op(question)[0] or "other"
        x = rules(op, question, entities, repo.as_of)
        _so_lines(x)
        infer(x)
        # 角色本來就不能做、或「全部零件」這種一定會被拒絕的，不必再請 Qwen3-VL 補欄位
        if account.can(x.op) and not x.scope_all:
            await fill_with_llm(x, question, repo.as_of)
            _so_lines(x)
    else:
        if not op:
            raise AppError("VALIDATION_ERROR", "請提供 question 或 op＋params", 422)
        x = Extraction(
            op=op, params=dict(params or {}), sources=dict.fromkeys(params or {}, "表單")
        )
    x.missing = missing(x)
    out = {
        "request_id": request_id,
        "op": x.op,
        "op_label": op_label(x.op),
        "account": account.public(),
        "params": {k: v for k, v in x.params.items() if k != "note"},
        "param_labels": labels(x.op, x.params),
        "sources": x.sources,
        "notes": x.notes,
        "llm": x.llm,
        "missing": x.missing,
        "checks": [],
        "diff": [],
        "reasons": [],
        "pending_id": None,
        "summary": "",
        "next": "need_info",
        "message": "",
    }

    def reject(message: str) -> dict:
        out["next"], out["message"] = "rejected", message
        _audit(
            account.id,
            account.label,
            "拒絕",
            x.op,
            out["summary"] or question or "",
            detail={"checks": out["checks"], "params": out["params"]},
            request_id=request_id,
        )
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        return out

    if x.op == "other":
        out["checks"] = authorize(account, "other", x.params)
        return reject("零件主檔與機台資料不開放用對話修改，請走 ERP 的正式流程")
    if x.scope_all:  # 「把所有庫存改成 0」：不管缺什麼欄位，範圍與上限就過不了
        out["summary"] = f"{op_label(x.op)}：全部零件"
        out["checks"] = checks = authorize(account, x.op, x.params, scope_all=True)
        if any(c["ok"] is False for c in checks):
            return reject(next(c["detail"] for c in checks if c["ok"] is False))
        n = repo.query("SELECT COUNT(*) AS n FROM stock")[0]["n"]
        checks.append(_limit(n, None))
        return reject(f"一次要改全部 {n} 筆庫存，超過單次上限")
    if x.missing:
        out["message"] = (
            "還缺："
            + "、".join(x.missing)
            + "。請把句子說完整，例如「一廠成品倉法蘭盤點少了 3 件」。"
        )
        # 角色不符的話，缺不缺欄位都不用再問了
        if not account.can(x.op):
            out["checks"] = authorize(account, x.op, x.params)
            return reject(out["checks"][0]["detail"])
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        return out
    try:
        p = normalize(x)
    except dc.ChangeError as e:
        out["checks"] = [{"key": "limit", "label": "上限", "ok": False, "detail": str(e)}]
        return reject(str(e))
    out["params"] = p
    out["param_labels"] = labels(x.op, p)
    out["notes"] = x.notes
    out["summary"] = summary(x.op, p)
    checks = authorize(account, x.op, p)
    out["checks"] = checks
    if any(c["ok"] is False for c in checks):
        return reject(next(c["detail"] for c in checks if c["ok"] is False))

    try:
        t = trial(x.op, p)
        checks.append(_limit(t["touched"], None))
    except dc.ChangeError as e:
        checks.append(_limit(0, str(e)))
        return reject(str(e))
    out["diff"] = t["diff"]
    if not checks[-1]["ok"]:
        return reject(checks[-1]["detail"])

    reasons = quota(x.op, p, t["before"])
    pending = Pending(
        id="chg_" + secrets.token_hex(8),
        op=x.op,
        params=p,
        account_id=account.id,
        fingerprint=t["fingerprint"],
        summary=out["summary"],
        diff=t["diff"],
        reasons=reasons,
        request_id=request_id,
    )
    _purge_pending()
    _pending[pending.id] = pending
    out.update(pending_id=pending.id, reasons=reasons, next="approval" if reasons else "confirm")
    out["message"] = (
        "超過額度，需要主管核准：" + "；".join(reasons)
        if reasons
        else "額度內：請核對修改前後差異後按確認"
    )
    out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


# ---------------------------------------------------------------- 5～6：寫入與讀回
def _next_no(prefix: str) -> str:
    yymm = date.today().strftime("%y%m")
    taken = get_production_repo().doc_numbers() | {
        r["ref_no"]
        for r in get_inventory_repo().query("SELECT DISTINCT ref_no FROM stock_moves")
        if r["ref_no"]
    }
    n = max(
        (int(t.rsplit("-", 1)[1]) for t in taken if t.startswith(f"{prefix}-{yymm}-")),
        default=0,
    )
    return f"{prefix}-{yymm}-{n + 1:02d}"


def _write(op: str, p: dict, actor: Account, approval: dict | None = None) -> str:
    """真正寫入（呼叫端持有 _write_lock）；回傳單號。"""
    prod = get_production_repo()
    if approval and op in dc.REPLAYED:
        # 異動紀錄的備註帶上申請說明與核准單號（Text-to-SQL 查得到來龍去脈）
        why = approval.get("request_note") or op_label(op)
        p = {**p, "note": f"{why}（{approval['ap_no']} 主管核准）"}
    if op in dc.REPLAYED:
        change_no = _next_no(get_agent_config()["modify_ops"][op]["prefix"])
        prod.add_change(
            {
                "change_no": change_no,
                "op": op,
                "params": p,
                "summary": summary(op, p),
                "moved_on": date.today().isoformat(),
                "actor_id": actor.id,
                "actor_label": actor.label,
                "approval_no": approval and approval["ap_no"],
                "approver_label": approval and approval["decided_label"],
            },
            approval=approval,
        )
    elif op == "wo_create":
        note = f"智慧助理開立（{actor.label}）" + (
            f"，{approval['ap_no']} 核准" if approval else ""
        )
        row = schedule_service.create_work_order(
            p["part_id"],
            int(p["qty"]),
            p["due_on"],
            p.get("priority", "一般"),
            p.get("release_on"),
            note,
            created_by=actor.id,
        )
        change_no = row["wo_no"]
        if approval:
            prod.decide_approval(
                approval["ap_no"],
                "已核准",
                approval["decided_by"],
                approval["decided_label"],
                approval.get("decision_note"),
                change_no,
            )
    elif op == "wo_cancel":
        schedule_service.cancel_work_order(p["wo_no"])
        change_no = p["wo_no"]
        if approval:
            prod.decide_approval(
                approval["ap_no"],
                "已核准",
                approval["decided_by"],
                approval["decided_label"],
                approval.get("decision_note"),
                change_no,
            )
    else:
        raise AppError("VALIDATION_ERROR", f"不在白名單的操作：{op}", 422)
    get_inventory_repo().ensure_built(force=True)
    return change_no


def read_back(op: str, p: dict, change_no: str, before_diff: list[dict]) -> dict:
    """從資料庫讀回實際寫入的結果，套固定模板回覆。"""
    repo = get_inventory_repo()
    mem = repo.copy_to_memory()
    try:
        after = dc.snapshot(
            mem, op, {**p, "wo_no": change_no if op == "wo_create" else p.get("wo_no")}
        )
        moves = mem.execute(
            "SELECT COUNT(*) FROM stock_moves WHERE ref_no = ?", (change_no,)
        ).fetchone()[0]
    finally:
        mem.close()
    rows = []
    after_by = {(s["label"], f): v for s in after for f, v in s["fields"].items()}
    for d in before_diff:
        label = d["label"] if op != "wo_create" else change_no
        rows.append({**d, "label": label, "after": after_by.get((label, d["field"]), d["after"])})
    if op == "wo_create":
        text = f"已開立 {change_no}：{summary(op, p).removeprefix('開立工單')}，等待排程。"
    elif op == "wo_cancel":
        text = f"已取消 {change_no}。"
    else:
        changes = "；".join(f"{r['label']} {r['field']} {r['before']} → {r['after']}" for r in rows)
        text = f"已寫入 {change_no}：{changes}。" + (f"（異動紀錄 {moves} 筆）" if moves else "")
    return {"change_no": change_no, "text": text, "rows": rows, "moves": moves}


def _fresh(op: str, p: dict) -> dict:
    """新工單的單號在寫入時才配：再驗時換成目前的下一個單號（指紋與單號無關）。"""
    return {**p, "wo_no": schedule_service.next_wo_no()[0]} if op == "wo_create" else p


def commit(pending_id: str, account: Account, request_id: str) -> dict:
    _purge_pending()
    pend = _pending.get(pending_id)
    if not pend:
        raise AppError("CHANGE_NOT_FOUND", "確認卡已過期或已使用，請重新輸入一次", 404)
    if pend.account_id != account.id:
        _audit(
            account.id,
            account.label,
            "拒絕寫入",
            pend.op,
            pend.summary,
            detail="確認卡不是目前身分建立的",
            request_id=request_id,
        )
        raise AppError(
            "PERMISSION_DENIED",
            f"這張確認卡是「{get_account(pend.account_id).label}」建立的，目前身分是「{account.label}」，請重新操作",
            403,
        )
    if pend.reasons:
        raise AppError(
            "APPROVAL_REQUIRED", "超過額度，需要送主管核准：" + "；".join(pend.reasons), 409
        )
    with _write_lock:
        checks = authorize(account, pend.op, pend.params)
        if any(c["ok"] is False for c in checks):
            _pending.pop(pending_id, None)
            reason = next(c["detail"] for c in checks if c["ok"] is False)
            _audit(
                account.id,
                account.label,
                "拒絕寫入",
                pend.op,
                pend.summary,
                detail={"checks": checks},
                request_id=request_id,
            )
            raise AppError("PERMISSION_DENIED", f"寫入前再驗權限不通過：{reason}", 403)
        get_inventory_repo().ensure_built(force=True)
        try:
            t = trial(pend.op, _fresh(pend.op, pend.params))
        except dc.ChangeError as e:
            t = {"fingerprint": "", "error": str(e)}
        if t["fingerprint"] != pend.fingerprint:
            _pending.pop(pending_id, None)
            _audit(
                account.id,
                account.label,
                "寫入失敗",
                pend.op,
                pend.summary,
                detail="資料已變動（指紋不同）",
                request_id=request_id,
            )
            raise AppError(
                "CHANGE_STALE",
                "從試算到按確認之間，這幾筆資料已被修改，沒有寫入。請重新操作一次。",
                409,
            )
        change_no = _write(pend.op, pend.params, account)
        _pending.pop(pending_id, None)
    result = read_back(pend.op, pend.params, change_no, pend.diff)
    _audit(
        account.id,
        account.label,
        "寫入",
        pend.op,
        pend.summary,
        change_no,
        detail={"params": pend.params},
        request_id=request_id,
    )
    log.info(
        "data_change", extra={"fields": {"change_no": change_no, "op": pend.op, "by": account.id}}
    )
    return {**result, "op": pend.op, "summary": pend.summary, "account": account.public()}


def direct(op: str, params: dict, account: Account, request_id: str, source: str) -> dict:
    """圖紙頁的「開立工單」、取消工單按鈕：不經對話，但權限、上限、額度用同一套規則。"""
    checks = authorize(account, op, params)
    if any(c["ok"] is False for c in checks):
        reason = next(c["detail"] for c in checks if c["ok"] is False)
        _audit(
            account.id,
            account.label,
            "拒絕",
            op,
            f"{op_label(op)}（{source}）",
            detail={"checks": checks, "source": source},
            request_id=request_id,
        )
        raise AppError("PERMISSION_DENIED", f"{reason}。請在頁首切換身分。", 403)
    if op == "wo_create" and (reasons := quota(op, params, [])):
        raise AppError(
            "APPROVAL_REQUIRED",
            f"{'；'.join(reasons)}：超過額度，需要主管核准。請按「送主管核准」，或改成一般工單。",
            409,
        )
    return {"checks": checks}


# ---------------------------------------------------------------- 主管核准
def request_approval(pending_id: str, account: Account, note: str | None, request_id: str) -> dict:
    _purge_pending()
    pend = _pending.get(pending_id)
    if not pend:
        raise AppError("CHANGE_NOT_FOUND", "確認卡已過期或已使用，請重新輸入一次", 404)
    if pend.account_id != account.id:
        raise AppError("PERMISSION_DENIED", "這張確認卡不是目前身分建立的，請重新操作", 403)
    if not pend.reasons:
        raise AppError("APPROVAL_NOT_NEEDED", "額度內，不需要主管核准，直接按確認即可", 409)
    ap_no = _next_no("AP")
    row = get_production_repo().add_approval(
        {
            "ap_no": ap_no,
            "op": pend.op,
            "params": pend.params,
            "summary": pend.summary,
            "reasons": pend.reasons,
            "diff": pend.diff,
            "fingerprint": pend.fingerprint,
            "note": note,
            "requester_id": account.id,
            "requester_label": account.label,
        }
    )
    _pending.pop(pending_id, None)
    _audit(
        account.id,
        account.label,
        "送核准",
        pend.op,
        pend.summary,
        ap_no,
        detail={"reasons": pend.reasons},
        request_id=request_id,
    )
    return _approval_view(row)


def _approval_view(ap: dict) -> dict:
    return {**ap, "op_label": op_label(ap["op"]), "param_labels": labels(ap["op"], ap["params"])}


def expire_old() -> None:
    hours = float(get_access_config()["approval"]["expire_hours"])
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    for ap_no in get_production_repo().expire_approvals(cutoff):
        _audit("system", "系統", "失效", None, f"{ap_no} 超過 {hours:g} 小時沒人處理", ap_no)


def list_approvals(account: Account) -> dict:
    expire_old()
    prod = get_production_repo()
    return {
        "can_approve": account.can("approve"),
        "pending": [_approval_view(a) for a in prod.approvals(status="待核准")],
        "mine": [_approval_view(a) for a in prod.approvals(requester_id=account.id, limit=20)],
        "recent": [_approval_view(a) for a in prod.approvals(limit=30)],
    }


def _open_approval(ap_no: str, account: Account, action: str, request_id: str) -> dict:
    expire_old()
    ap = get_production_repo().get_approval(ap_no)
    if not ap:
        raise AppError("APPROVAL_NOT_FOUND", f"找不到待核准單 {ap_no}", 404)
    if ap["status"] != "待核准":
        raise AppError("APPROVAL_CLOSED", f"{ap_no} 已經是「{ap['status']}」", 409)
    reason = None
    if not account.can("approve"):
        reason = f"「{account.label}」沒有核准權限（只有主管可以）"
    elif ap["requester_id"] == account.id:
        reason = "不能核准自己的申請"
    if reason:
        _audit(
            account.id,
            account.label,
            f"拒絕{action}",
            ap["op"],
            ap["summary"],
            ap_no,
            detail=reason,
            request_id=request_id,
        )
        raise AppError("PERMISSION_DENIED", reason, 403)
    return ap


def approve(ap_no: str, account: Account, note: str | None, request_id: str) -> dict:
    ap = _open_approval(ap_no, account, "核准", request_id)
    prod = get_production_repo()
    requester = get_account(ap["requester_id"])
    with _write_lock:
        get_inventory_repo().ensure_built(force=True)
        # 申請人的權限可能在申請後被收回：核准前再驗一次申請人的權限
        checks = authorize(requester, ap["op"], ap["params"])
        problem = next((c["detail"] for c in checks if c["ok"] is False), None)
        if not problem:
            try:
                t = trial(ap["op"], _fresh(ap["op"], ap["params"]))
                if t["fingerprint"] != ap["fingerprint"]:
                    problem = "申請後這幾筆資料已被修改，試算結果和申請時不同"
            except dc.ChangeError as e:
                problem = str(e)
        if problem:
            prod.decide_approval(ap_no, "已失效", account.id, account.label, problem)
            _audit(
                account.id,
                account.label,
                "失效",
                ap["op"],
                ap["summary"],
                ap_no,
                detail=problem,
                request_id=request_id,
            )
            return {
                "ap_no": ap_no,
                "status": "已失效",
                "text": f"{ap_no} 已失效：{problem}。請申請人重新送出。",
            }
        approval = {
            "ap_no": ap_no,
            "decided_by": account.id,
            "decided_label": account.label,
            "decision_note": note,
            "request_note": ap.get("note"),
        }
        change_no = _write(ap["op"], ap["params"], requester, approval)
    result = read_back(ap["op"], ap["params"], change_no, ap["diff"])
    _audit(
        account.id,
        account.label,
        "核准寫入",
        ap["op"],
        ap["summary"],
        change_no,
        detail={"ap_no": ap_no, "requester": requester.label},
        request_id=request_id,
    )
    return {"ap_no": ap_no, "status": "已核准", **result}


def return_(ap_no: str, account: Account, reason: str, request_id: str) -> dict:
    if not reason.strip():
        raise AppError("VALIDATION_ERROR", "退回要附理由", 422)
    ap = _open_approval(ap_no, account, "退回", request_id)
    get_production_repo().decide_approval(ap_no, "已退回", account.id, account.label, reason)
    _audit(
        account.id,
        account.label,
        "退回",
        ap["op"],
        ap["summary"],
        ap_no,
        detail=reason,
        request_id=request_id,
    )
    return {"ap_no": ap_no, "status": "已退回", "text": f"{ap_no} 已退回：{reason}"}
