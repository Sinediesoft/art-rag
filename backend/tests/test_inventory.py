"""工廠庫存 Text-to-SQL：示範資料一致性、SQL 安全檢查、API 與 SSE（mock 模型）。"""

import pytest
from test_api import parse_sse

from app.rag import text2sql
from app.rag.kb import validate_inventory, validate_parts
from app.repositories.inventory_repo import SqlError, get_inventory_repo, prompt_schema


@pytest.fixture(scope="module")
def repo(mock_env):
    r = get_inventory_repo()
    r.ensure_built()
    return r


def test_seed_covers_six_drawings_and_is_consistent(repo):
    parts, _ = validate_parts(check_drawing=False)
    site, items, errors = validate_inventory({p["id"] for p in parts})
    assert errors == [] and site["as_of"] == repo.as_of
    assert {it["part_id"] for it in items} == {f"mfg-00{i}" for i in range(1, 7)}
    # 各倉異動加總＝目前庫存（資料庫裡再算一次）
    rows = repo._query(
        """SELECT s.part_id, s.warehouse_id, s.total,
             (SELECT SUM(qty) FROM stock_moves m
              WHERE m.part_id = s.part_id AND m.warehouse_id = s.warehouse_id) AS moved
           FROM (SELECT part_id, warehouse_id, SUM(qty) AS total FROM stock
                 GROUP BY part_id, warehouse_id) s"""
    )
    assert rows and all(r["total"] == r["moved"] for r in rows)
    # 訂單已出貨量＝銷貨出庫
    rows = repo._query(
        """SELECT o.so_no, SUM(o.qty_shipped) AS shipped,
             COALESCE((SELECT -SUM(qty) FROM stock_moves m
                       WHERE m.move_type = '銷貨出庫' AND m.ref_no = o.so_no), 0) AS out
           FROM sales_orders o GROUP BY o.so_no"""
    )
    assert all(r["shipped"] == r["out"] for r in rows)


def test_demo_scenarios_exist(repo):
    """展示用情境：低於安全庫存、訂單缺貨、不良品。"""
    low = {r["part_id"] for r in repo.overview() if r["available"] < r["safety_stock"]}
    assert low == {"mfg-002", "mfg-003"}
    short = repo.run_readonly(
        """SELECT o.so_no FROM sales_orders o JOIN v_part_stock v ON v.part_id = o.part_id
           WHERE o.status != '已出貨' AND o.qty - o.qty_shipped > v.available""",
        50,
        1000,
    )
    assert len(short.rows) == 3
    assert repo.part_inventory("mfg-001")["defective"] == 3


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM stock",
        "SELECT 1; DROP TABLE stock",
        "UPDATE parts SET std_cost_twd = 0",
        "PRAGMA table_info(stock)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "WITH x AS (SELECT 1) INSERT INTO stock (part_id) SELECT 'a' FROM x",
        "SELECT load_extension('evil')",
    ],
)
def test_check_sql_rejects_writes_and_admin(sql):
    with pytest.raises(text2sql.SqlRejected):
        text2sql.check_sql(sql)


def test_check_sql_accepts_select_and_ignores_literals():
    assert text2sql.check_sql("SELECT note FROM stock WHERE note LIKE '%DELETE;%';") == (
        "SELECT note FROM stock WHERE note LIKE '%DELETE;%'"
    )
    assert text2sql.check_sql("with t as (select 1 as a) select a from t").startswith("with")


def test_extract_sql_from_model_output():
    raw = "好的，查詢如下：\n```sql\nSELECT name FROM parts;\n```\n這會列出品名。"
    assert text2sql.extract_sql(raw) == "SELECT name FROM parts"
    assert text2sql.extract_sql("SELECT 1; SELECT 2") == "SELECT 1"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT sql FROM sqlite_schema",
        "WITH m AS (SELECT * FROM sqlite_master) SELECT * FROM m",
        "SELECT randomblob(10)",
    ],
)
def test_authorizer_blocks_internal_tables_and_functions(repo, sql):
    """靜態檢查漏掉的，執行層的 authorizer 白名單也會擋下。"""
    with pytest.raises(SqlError):
        repo.run_readonly(sql, 10, 1000)


def test_runaway_query_is_interrupted_and_rows_capped(repo):
    with pytest.raises(SqlError, match="中止"):
        repo.run_readonly(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c)"
            " SELECT COUNT(*) FROM c",
            10,
            300,
        )
    r = repo.run_readonly("SELECT * FROM stock_moves", 5, 1000)
    assert len(r.rows) == 5 and r.truncated


def test_inventory_endpoints(client):
    schema = client.get("/api/v1/inventory/schema").json()
    names = {t["name"] for t in schema["tables"]}
    assert {"parts", "stock", "stock_moves", "work_orders", "sales_orders", "v_part_stock"} <= names
    assert {"machines", "schedule_ops", "v_wo_plan"} <= names
    assert schema["examples"]
    items = client.get("/api/v1/inventory/overview").json()["items"]
    assert len(items) == 6
    part = client.get("/api/v1/inventory/parts/mfg-006").json()
    assert part["name"] == "立式軸承座" and part["locations"] and part["sales_orders"]
    assert client.get("/api/v1/inventory/parts/nope-1").status_code == 404


def test_ask_streams_sql_result_answer(client):
    r = client.post("/api/v1/inventory/ask", json={"question": "哪些零件的可用庫存低於安全庫存？"})
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert {"attempt", "sql_token", "sql", "result", "token"} <= set(kinds)
    result = next(d for e, d in events if e == "result")
    assert {row[0] for row in result["rows"]} == {"連接法蘭", "階梯傳動軸"}
    done = events[-1][1]
    assert done["egress"] == {"images": 0, "chunks": 0, "bytes": 0}
    assert done["prompt_version"] == "sql_v2" and done["attempts"] == 1
    health = client.get("/api/v1/admin/diagnostics").json()  # 最近的 SQL 只在管理診斷
    assert health["inventory"]["ok"] and health["recent_sql"][0]["ok"] == 1


def test_ask_rejects_cloud_strategies(client):
    for strategy in ("api_nokb", "api_kb"):
        r = client.post(
            "/api/v1/inventory/ask", json={"question": "庫存總價值", "strategy": strategy}
        )
        events = parse_sse(r.text)
        assert len(events) == 1 and events[0][0] == "error"
        assert events[0][1]["code"] == "CLOUD_CONFIDENTIAL_FORBIDDEN"


def test_prompt_contains_schema_values_and_question(repo):
    msgs = text2sql.build_sql_messages(
        "法蘭還有幾件？", prompt_schema(), repo.value_hints(), repo.as_of
    )
    user = msgs[1]["content"]
    assert "CREATE TABLE stock" in user and "連接法蘭" in user and user.endswith("法蘭還有幾件？")
    assert repo.as_of in msgs[0]["content"]


def test_ask_repairs_sql_with_error_feedback(client, monkeypatch):
    """第一次產生的 SQL 欄位不存在 → 錯誤訊息回饋給模型 → 第二次修正成功。"""
    outputs = iter(
        ["```sql\nSELECT qty_available FROM stock\n```", "SELECT name AS 品名 FROM parts"]
    )
    monkeypatch.setattr(text2sql, "mock_sql", lambda q, h: next(outputs))
    events = parse_sse(client.post("/api/v1/inventory/ask", json={"question": "品名"}).text)
    sqls = [d for e, d in events if e == "sql"]
    assert [s["ok"] for s in sqls] == [False, True]
    assert "no such column" in sqls[0]["error"]
    assert [d["n"] for e, d in events if e == "attempt"] == [1, 2]
    assert events[-1][0] == "done" and events[-1][1]["attempts"] == 2


def test_ask_gives_up_after_max_repairs(client, monkeypatch):
    monkeypatch.setattr(text2sql, "mock_sql", lambda q, h: "SELECT nope FROM stock")
    events = parse_sse(client.post("/api/v1/inventory/ask", json={"question": "?"}).text)
    sqls = [d for e, d in events if e == "sql"]
    assert len(sqls) == 3 and not any(s["ok"] for s in sqls)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "SQL_FAILED"


def test_write_request_is_rejected_without_repair(client, monkeypatch):
    """模型寫出 UPDATE：直接拒絕，不進修正迴圈、不產生「已修改」的回答，資料不變。"""
    before = client.get("/api/v1/inventory/overview").json()["items"]
    monkeypatch.setattr(text2sql, "mock_sql", lambda q, h: "UPDATE stock SET qty = 0")
    events = parse_sse(client.post("/api/v1/inventory/ask", json={"question": "全改成 0"}).text)
    kinds = [e for e, _ in events]
    assert kinds.count("sql") == 1 and "token" not in kinds and "result" not in kinds
    assert events[-1][0] == "error" and events[-1][1]["code"] == "SQL_REJECTED"
    assert client.get("/api/v1/inventory/overview").json()["items"] == before


def test_empty_result_answers_without_model(client, monkeypatch):
    monkeypatch.setattr(
        text2sql, "mock_sql", lambda q, h: "SELECT name FROM parts WHERE part_id = 'none'"
    )
    events = parse_sse(client.post("/api/v1/inventory/ask", json={"question": "x"}).text)
    assert "".join(d["text"] for e, d in events if e == "token") == "查無符合條件的資料。"
