"""修改資料的參數抽取：先用規則（正規表示式＋名稱對照），缺的欄位再請本地 Qwen3-VL 輸出 JSON 補上。

Jev 只判斷意圖與操作類型，不抽數字、不算日期。抽出的參數一律再對照資料庫的值清單驗證，
不在清單裡的值（模型亂寫的倉庫、零件）直接丟掉，交給確認卡讓使用者核對。
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date

from app.agent.entities import Entity, first
from app.core.config import get_agent_config, get_settings
from app.core.logging import log
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.inventory_repo import get_inventory_repo

STATUSES = ("可用", "保留", "待檢", "不良")

# 每種操作必填的欄位；a|b 代表兩者擇一
REQUIRED: dict[str, list[str]] = {
    "stock_adjust": ["part_id", "warehouse_id", "delta|counted"],
    "stock_transfer": ["part_id", "from_warehouse_id", "to_warehouse_id", "qty"],
    "stock_scrap": ["part_id", "warehouse_id", "qty"],
    "stock_status": ["part_id", "warehouse_id", "qty", "to_status"],
    "so_update": ["so_no", "line_no", "due_on|qty"],
    "wo_create": ["part_id", "qty", "due_on"],
    "wo_update_due": ["wo_no", "due_on"],
    "wo_cancel": ["wo_no"],
    "other": [],
}
FIELD_LABELS = {
    "part_id": "零件",
    "warehouse_id": "倉庫",
    "from_warehouse_id": "調出倉庫",
    "to_warehouse_id": "調入倉庫",
    "qty": "數量",
    "delta": "增減數量",
    "counted": "實盤數量",
    "status": "庫存狀態",
    "from_status": "原狀態",
    "to_status": "新狀態",
    "so_no": "訂單號",
    "line_no": "訂單項次",
    "due_on": "交期",
    "wo_no": "工單號",
    "priority": "優先",
    "customer": "客戶",
    "release_on": "可開工日",
    "work_order": "新工單",
}

QTY = re.compile(r"(\d+)\s*(?:件|個|pcs|PCS|支|片)")
COUNTED = re.compile(r"(?:改成|改為|調整為|設為|設成|盤到|實盤|只剩|剩下|剩)\s*(\d+)")
LOSS = re.compile(r"(?:少了|短少|盤虧|少|缺)\s*(\d+)")
GAIN = re.compile(r"(?:多了|盤盈|多)\s*(\d+)")
DECREASE = re.compile(r"(?:減少|減|少訂|砍)\s*(\d+)")
INCREASE = re.compile(r"(?:增加|加訂|多訂|追加)\s*(\d+)")
ISO_DATE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
MD_DATE = re.compile(r"(?<![\d-])(\d{1,2})\s*(?:/|月)\s*(\d{1,2})\s*(?:日|號)?(?![\d-])")
LINE_NO = re.compile(r"第\s*(\d+)\s*(?:項|列|行)")
RUSH = re.compile(r"急件|急單|趕工|加急|插單|緊急")
ALL = re.compile(
    r"(所有|全部|每個|每一個|全都|通通|一律).{0,8}(庫存|零件|品項|料號)|(庫存|零件).{0,4}(全部|通通|都)"
)
TO_STATUS = re.compile(r"(?:改成|改為|轉為|轉成|設為|設成|標成|標為|轉)\s*(可用|保留|待檢|不良)")
TO_MARK = re.compile(r"(?:到|至|進|往|入)\s*$")


@dataclass
class Extraction:
    op: str
    params: dict = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # 欄位 → 規則／推定／Qwen3-VL
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    scope_all: bool = False
    llm: dict | None = None  # Qwen3-VL 輸出的 JSON（畫面上可展開看）


def _date(text: str, as_of: str) -> str | None:
    year = int(as_of[:4]) if as_of else date.today().year
    if m := ISO_DATE.search(text):
        y, mo, d = map(int, m.groups())
    elif m := MD_DATE.search(text):
        y, (mo, d) = year, map(int, m.groups())
    else:
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _set(x: Extraction, key: str, value, source: str = "規則") -> None:
    if value is not None and value != "" and key not in x.params:
        x.params[key] = value
        x.sources[key] = source


def _warehouses(text: str, entities: list[Entity], op: str) -> list[tuple[str, str]]:
    """句子裡的倉庫（依出現順序）：[(倉庫代碼, 角色 from／to／'')]。

    只說廠區（「一廠」）就用 agent.yaml 的預設倉。
    """
    sites = get_agent_config()["aliases"]["sites"]
    out = []
    for e in entities:
        if e.kind == "warehouse":
            wh = e.id
        elif e.kind == "site":
            pick = "defective" if op == "stock_scrap" and "不良" in text else "default"
            wh = sites.get(e.id, {}).get(pick)
            if not wh:
                continue
        else:
            continue
        role = "to" if TO_MARK.search(text[max(0, e.start - 3) : e.start]) else ""
        role = "from" if not role and "從" in text[max(0, e.start - 3) : e.start] else role
        out.append((wh, role, e.kind == "site"))
    return [(w, r) for w, r, _ in out] if out else []


def _status_word(text: str, default: str) -> str:
    for s in ("不良", "待檢", "保留"):
        if s in text:
            return s
    return default


def rules(op: str, text: str, entities: list[Entity], as_of: str) -> Extraction:
    x = Extraction(op=op)
    # 判斷庫存狀態時去掉倉庫名稱：「不良品隔離區」不代表要動「不良」的庫存
    pieces, pos = [], 0
    for e in entities:
        if e.kind == "warehouse":
            pieces.append(text[pos : e.start])
            pos = e.end
    plain = "".join(pieces) + text[pos:]
    part = first(entities, "part")
    if part:
        _set(x, "part_id", part.id)
    elif op.startswith("stock_") and ALL.search(text):
        x.scope_all = True
        x.params["part_id"] = "*"
        x.sources["part_id"] = "規則"
    whs = _warehouses(text, entities, op)
    if any(e.kind == "site" for e in entities) and not any(e.kind == "warehouse" for e in entities):
        x.notes.append("只說了廠區：依 shared/agent.yaml 用該廠的預設倉庫")
    qty = QTY.search(text)
    due = _date(text, as_of)

    if op == "stock_adjust":
        if whs:
            _set(x, "warehouse_id", whs[0][0])
        _set(x, "status", _status_word(plain, "可用"))
        if m := COUNTED.search(text):
            _set(x, "counted", int(m.group(1)))
        elif m := LOSS.search(text):
            _set(x, "delta", -int(m.group(1)))
        elif m := GAIN.search(text):
            _set(x, "delta", int(m.group(1)))
    elif op == "stock_transfer":
        to = [w for w, r in whs if r == "to"]
        frm = [w for w, r in whs if r != "to"]
        if to:
            _set(x, "to_warehouse_id", to[0])
        elif len(frm) >= 2:
            _set(x, "to_warehouse_id", frm.pop(1))
        if frm:
            _set(x, "from_warehouse_id", frm[0])
        _set(x, "status", _status_word(plain, "可用"))
        if qty:
            _set(x, "qty", int(qty.group(1)))
    elif op == "stock_scrap":
        if whs:
            _set(x, "warehouse_id", whs[0][0])
        _set(x, "status", _status_word(plain, "可用"))
        if qty:
            _set(x, "qty", int(qty.group(1)))
    elif op == "stock_status":
        if whs:
            _set(x, "warehouse_id", whs[0][0])
        if m := TO_STATUS.search(text):
            _set(x, "to_status", m.group(1))
        elif re.search(r"檢驗合格|放行", text):
            _set(x, "to_status", "可用")
            _set(x, "from_status", "待檢")
        elif "判定不良" in text:
            _set(x, "to_status", "不良")
        elif "解除保留" in text:
            _set(x, "to_status", "可用")
            _set(x, "from_status", "保留")
        to = x.params.get("to_status")
        before = text[: TO_STATUS.search(text).start()] if TO_STATUS.search(text) else text
        explicit = next((s for s in STATUSES if s in before and s != to), None)
        default_from = {"可用": "待檢", "不良": "待檢", "待檢": "可用", "保留": "可用"}.get(
            to or ""
        )
        _set(x, "from_status", explicit or default_from, "規則" if explicit else "推定")
        if qty:
            _set(x, "qty", int(qty.group(1)))
    elif op == "so_update":
        if so := first(entities, "so"):
            _set(x, "so_no", so.id)
        if m := LINE_NO.search(text):
            _set(x, "line_no", int(m.group(1)))
        _set(x, "due_on", due)
        if m := DECREASE.search(text):
            x.params["qty_delta"] = -int(m.group(1))
        elif m := INCREASE.search(text):
            x.params["qty_delta"] = int(m.group(1))
        elif m := re.search(
            r"數量\s*(?:改成|改為|調整為|設為)?\s*(\d+)|(?:改成|改為)\s*(\d+)\s*件", text
        ):
            _set(x, "qty", int(m.group(1) or m.group(2)))
    elif op == "wo_create":
        if qty:
            _set(x, "qty", int(qty.group(1)))
        _set(x, "due_on", due)
        _set(x, "priority", "急件" if RUSH.search(text) else "一般")
    elif op in ("wo_update_due", "wo_cancel"):
        if wo := first(entities, "wo"):
            _set(x, "wo_no", wo.id)
        if op == "wo_update_due":
            _set(x, "due_on", due)
    return x


def infer(x: Extraction) -> None:
    """規則沒抽到的倉庫，用資料庫推定：這個零件在該狀態只有一個倉庫有貨，就用那一個。"""
    repo = get_inventory_repo()
    pid = x.params.get("part_id")
    if not pid or pid == "*" or not x.op.startswith("stock_"):
        return
    key = "from_warehouse_id" if x.op == "stock_transfer" else "warehouse_id"
    if key in x.params:
        return
    status = x.params.get("from_status") or x.params.get("status") or "可用"
    rows = repo.query(
        "SELECT warehouse_id, SUM(qty) AS qty FROM stock WHERE part_id = ? AND status = ?"
        " GROUP BY warehouse_id HAVING SUM(qty) > 0",
        (pid, status),
    )
    if len(rows) == 1:
        _set(x, key, rows[0]["warehouse_id"], "推定")
        x.notes.append(f"沒說倉庫：「{status}」庫存只在 {rows[0]['warehouse_id']}，就用這個倉庫")


def missing(x: Extraction) -> list[str]:
    out = []
    for f in REQUIRED.get(x.op, []):
        alts = f.split("|")
        if not any(a in x.params or (a == "qty" and "qty_delta" in x.params) for a in alts):
            out.append(" 或 ".join(FIELD_LABELS.get(a, a) for a in alts))
    return out


# ---------------------------------------------------------------- 本地 Qwen3-VL 補欄位
LLM_SYSTEM = (
    "你是工廠系統的參數抽取器。依使用者的一句話，輸出這次修改需要的欄位，只輸出一個 JSON 物件，"
    "不要任何說明。沒提到的欄位不要猜，填 null。值只能用下面清單裡的代碼。"
)


def _llm_prompt(x: Extraction, text: str, need: list[str], as_of: str) -> str:
    hints = get_inventory_repo().value_hints()
    parts = "、".join(f"{p['part_id']}＝{p['name']}" for p in hints["parts"])
    whs = "、".join(f"{w['warehouse_id']}＝{w['name']}" for w in hints["warehouses"])
    keys = sorted({k for f in need for k in f.split("|")})
    return (
        f"操作：{x.op}\n資料日期：{as_of}（日期一律輸出 YYYY-MM-DD）\n"
        f"零件代碼：{parts}\n倉庫代碼：{whs}\n庫存狀態：可用、保留、待檢、不良\n"
        f"訂單號格式 SO-YYMM-NNN；工單號格式 WO-YYMM-NN\n"
        f"需要的欄位：{', '.join(keys)}（數量為整數；delta 為增減量，減少用負數）\n"
        f"已知：{json.dumps(x.params, ensure_ascii=False)}\n使用者：{text}\nJSON："
    )


def _valid(key: str, value) -> object | None:
    hints = get_inventory_repo().value_hints()
    if value in (None, "", "null"):
        return None
    if key == "part_id":
        return value if value in {p["part_id"] for p in hints["parts"]} else None
    if key.endswith("warehouse_id"):
        return value if value in {w["warehouse_id"] for w in hints["warehouses"]} else None
    if key.endswith("status"):
        return value if value in STATUSES else None
    if key in ("qty", "counted", "line_no"):
        return int(value) if str(value).lstrip("-").isdigit() and int(value) >= 0 else None
    if key == "delta":
        return int(value) if str(value).lstrip("-").isdigit() else None
    if key == "due_on":
        return value if isinstance(value, str) and ISO_DATE.fullmatch(value) else None
    if key in ("so_no", "wo_no"):
        return (
            value.upper()
            if isinstance(value, str) and re.fullmatch(r"(SO|WO)-\d{4}-\d{2,3}", value, re.I)
            else None
        )  # noqa: E501
    return None


async def fill_with_llm(x: Extraction, text: str, as_of: str) -> None:
    """缺欄位時才呼叫本地 Qwen3-VL（hybrid → 本地備援；mock 模式不呼叫）。"""
    need = missing(x)
    if not need or get_settings().llm_mode == "mock":
        return
    keys = [k for f in REQUIRED.get(x.op, []) for k in f.split("|") if k not in x.params]
    t0 = time.perf_counter()
    messages = [
        {"role": "system", "content": LLM_SYSTEM},
        {"role": "user", "content": _llm_prompt(x, text, keys, as_of)},
    ]
    raw = ""
    for strategy in ("hybrid", "hybrid_fallback"):
        try:
            provider = get_provider(strategy)
            provider.max_tokens, provider.temperature = 200, 0.0
            raw = "".join([piece async for piece in provider.stream(messages)])
            break
        except ProviderUnavailable as e:
            log.info(f"參數抽取：{strategy} 無法使用（{e}）")
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    x.llm = {"json": data, "ms": round((time.perf_counter() - t0) * 1000), "raw": raw[:400]}
    for k in keys:
        if (v := _valid(k, data.get(k))) is not None:
            _set(x, k, v, "Qwen3-VL")
