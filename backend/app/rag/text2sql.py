"""Text-to-SQL：中文問題 → SQLite 查詢 → 依查詢結果回答。

- prompt：shared/prompts/sql_v2.md（產生 SQL）與 sql_answer_v2.md（依結果回答），
  few-shot 範例在 sql_v2_examples.json；schema 與欄位值（零件、倉庫、客戶、機台）由資料庫即時產生
- check_sql：執行前的靜態檢查（只准一條 SELECT／WITH、擋寫入與管理指令）；
  真正的保護在 repositories/inventory_repo.run_readonly（唯讀連線＋authorizer 白名單＋逾時）
- mock_sql／mock_answer：LLM_MODE=mock 時不呼叫模型，從範例挑最接近的 SQL
"""

import json
import re
from functools import lru_cache

from app.core.config import get_models_config, get_settings
from app.rag.prompt import load_template


class SqlRejected(Exception):
    """靜態檢查不通過。

    write=True 代表含寫入或管理指令（使用者想改資料）：直接拒絕、不讓模型「修正」，
    否則模型會改寫成 SELECT，回答時還可能誤稱「已修改」。
    """

    def __init__(self, message: str, write: bool = False):
        super().__init__(message)
        self.write = write


FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE|"
    r"BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|LOAD_EXTENSION|REPLACE\s+INTO)\b",
    re.I,
)


def prompt_versions() -> tuple[str, str]:
    p = get_models_config().prompt
    return p.get("sql_version", "sql_v1"), p.get("sql_answer_version", "sql_answer_v1")


@lru_cache
def load_examples(version: str) -> list[dict]:
    path = get_settings().shared_dir / "prompts" / f"{version}_examples.json"
    return json.loads(path.read_text(encoding="utf-8"))


def format_values(hints: dict) -> str:
    parts = "、".join(f"{p['part_id']}＝{p['part_no']}〈{p['name']}〉" for p in hints["parts"])
    whs = "、".join(f"{w['warehouse_id']}＝{w['name']}（{w['kind']}）" for w in hints["warehouses"])
    machines = "、".join(
        f"{m['machine_id']}（{m['machine_type']}）" for m in hints.get("machines", [])
    )
    return "\n".join(
        [
            f"- 零件（品名要完全照抄，含空格）：{parts}",
            f"- 倉庫：{whs}",
            f"- 客戶：{'、'.join(hints['customers'])}",
            *([f"- 機台（machine_id 要完全照抄，含空格）：{machines}"] if machines else []),
            "- stock.status：可用、保留、待檢、不良",
            "- stock_moves.move_type：期初、生產入庫、銷貨出庫、調撥入、調撥出、報廢、盤點調整",
            "- work_orders.status：已開立、生產中、委外處理中、已完工",
            "- work_orders.priority：一般、急件",
            "- schedule_ops.kind：自製、委外；v_wo_plan.on_time：準時、延遲、未排程",
            "- sales_orders.status：待出貨、部分出貨、已出貨",
        ]
    )


def build_sql_messages(question: str, schema: str, hints: dict, as_of: str) -> list[dict]:
    version = prompt_versions()[0]
    tpl = load_template(version)
    examples = "\n\n".join(
        f"問題：{e['question']}\n```sql\n{e['sql']}\n```" for e in load_examples(version)
    )
    fill = {
        "{{as_of}}": as_of,
        "{{schema}}": schema,
        "{{values}}": format_values(hints),
        "{{examples}}": examples,
        "{{question}}": question,
    }
    system, user = tpl["system"], tpl["user"]
    for k, v in fill.items():
        system, user = system.replace(k, v), user.replace(k, v)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def repair_message(error: str) -> dict:
    return {
        "role": "user",
        "content": f"上面這條 SQL 無法執行，錯誤訊息：{error}\n"
        "請對照資料表與欄位修正，只輸出修正後的一條 SQL（放在 ```sql 區塊中）。",
    }


def _mask_literals(sql: str) -> str:
    """把字串常值與引號識別字換成空白，之後的關鍵字檢查才不會誤判 '保留給 DELETE' 這種內容。"""
    return re.sub(
        r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`", lambda m: " " * len(m.group()), sql
    )


def extract_sql(text: str) -> str:
    """從模型輸出取出 SQL：優先取 ``` 區塊，否則從第一個 SELECT／WITH 開始；只取第一條。"""
    fenced = re.search(r"```(?:sql|sqlite)?\s*\n?(.*?)(?:```|$)", text, flags=re.S | re.I)
    body = fenced.group(1) if fenced else text
    start = re.search(r"\b(SELECT|WITH)\b", body, flags=re.I)
    if start:
        body = body[start.start() :]
    masked = _mask_literals(body)
    if ";" in masked:
        body = body[: masked.index(";")]
    return body.strip()


def check_sql(sql: str) -> str:
    sql = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if not sql:
        raise SqlRejected("模型沒有產生 SQL")
    masked = _mask_literals(sql)
    if m := FORBIDDEN.search(masked):
        raise SqlRejected(f"不允許 {m.group(1).upper()}：庫存查詢只能讀取資料", write=True)
    if ";" in masked:
        raise SqlRejected("一次只能執行一條 SQL")
    if not re.match(r"(SELECT|WITH)\b", masked.lstrip(), flags=re.I):
        raise SqlRejected("只能執行 SELECT 查詢（可用 WITH 開頭）")
    return sql


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


def format_table(columns: list[str], rows: list[list], limit: int) -> str:
    lines = [" | ".join(columns)]
    lines += [" | ".join(cell(v) for v in r) for r in rows[:limit]]
    return "\n".join(lines)


def build_answer_messages(
    question: str, sql: str, columns: list[str], rows: list[list], truncated: bool, as_of: str
) -> list[dict]:
    tpl = load_template(prompt_versions()[1])
    limit = int(get_models_config().text2sql.get("prompt_rows", 30))
    note = f"共 {len(rows)}{' 筆以上' if truncated else ' 筆'}"
    if len(rows) > limit:
        note += f"，只列前 {limit} 筆"
    fill = {
        "{{as_of}}": as_of,
        "{{question}}": question,
        "{{sql}}": sql,
        "{{row_note}}": note,
        "{{table}}": format_table(columns, rows, limit),
    }
    system, user = tpl["system"], tpl["user"]
    for k, v in fill.items():
        system, user = system.replace(k, v), user.replace(k, v)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------- mock（不呼叫模型）
def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s？?，,。、「」]", "", s)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def mock_sql(question: str, hints: dict) -> str:
    """從 few-shot 範例挑字面最接近的一條；問題提到的零件名稱代入 LIKE 條件。"""
    q = _bigrams(question)
    best, score = None, 0.0
    for e in load_examples(prompt_versions()[0]):
        b = _bigrams(e["question"])
        s = len(q & b) / (len(q | b) or 1)
        if s > score:
            best, score = e, s
    if best is None or score < 0.12:
        return (
            "SELECT name AS 品名, available AS 可用, reserved AS 保留, inspecting AS 待檢, "
            "defective AS 不良, safety_stock AS 安全庫存 FROM v_part_stock ORDER BY part_id"
        )
    sql = best["sql"]
    for p in hints["parts"]:
        key = p["name"].split()[-1]  # 「T 型槽螺帽」→「型槽螺帽」也比得到「T型槽螺帽」
        if key in question.replace(" ", "") or p["part_no"] in question:
            sql = re.sub(r"p\.name LIKE '%[^%']*%'", f"p.name LIKE '%{key}%'", sql)
            sql = re.sub(r"p\.part_no = '[^']*'", f"p.part_no = '{p['part_no']}'", sql)
            break
    return sql


def mock_answer(columns: list[str], rows: list[list], truncated: bool) -> str:
    shown = ["、".join(f"{c} {cell(v)}" for c, v in zip(columns, r, strict=True)) for r in rows[:3]]
    more = f"等 {len(rows)}{' 筆以上' if truncated else ' 筆'}" if len(rows) > 3 else ""
    return f"（mock 模式）查詢結果共 {len(rows)} 筆：{'；'.join(shown)}{more}。"
