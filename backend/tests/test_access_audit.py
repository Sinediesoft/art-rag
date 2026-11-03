"""機密資料的讀取稽核：讀機密圖紙、3D 模型、明細、問答都記下是哪個帳號；
主管在 /audit 的 reads 看得到。
內部人員演練：原本稽核只記修改，伺服器 log 只有「127.0.0.1 下載了哪個檔案」，查不出是誰。"""

from conftest import as_account

SECRET, INTERNAL = "mfg-001", "mfg-004"  # 機密、內部


def _reads(client) -> list[dict]:
    as_account(client, "manager")
    return client.get("/api/v1/audit?limit=200").json()["reads"]


def test_secret_reads_are_recorded_with_the_account(client):
    before = len(_reads(client))
    as_account(client, "warehouse")
    for url in [
        f"/api/v1/parts/{SECRET}",
        f"/api/v1/parts/{SECRET}/drawing",
        f"/api/v1/parts/{SECRET}/model.step",
        f"/api/v1/inventory/parts/{SECRET}",
        f"/api/v1/production/parts/{SECRET}",
    ]:
        assert client.get(url).status_code == 200, url
    rows = _reads(client)[: len(_reads(client)) - before]
    mine = [r for r in rows if r["account_id"] == "warehouse" and r["part_id"] == SECRET]
    assert {r["what"] for r in mine} == {
        "圖紙明細",
        "原尺寸圖紙",
        "STEP 模型",
        "庫存明細",
        "生產明細",
    }
    assert all(r["level"] == "機密" and r["account_label"] == "倉管" for r in mine)


def test_internal_parts_thumbnails_and_denied_reads_are_not_recorded(client):
    before = len(_reads(client))
    as_account(client, "warehouse")
    client.get(f"/api/v1/parts/{INTERNAL}/drawing")  # 內部等級：不記
    client.get(f"/api/v1/parts/{SECRET}/drawing?size=thumb")  # 縮圖：不記
    as_account(client, "sales")
    assert client.get(f"/api/v1/parts/{SECRET}/drawing").status_code == 403  # 被擋：不算讀到
    assert len(_reads(client)) == before


def test_drawing_qa_on_a_secret_part_is_recorded(client):
    before = len(_reads(client))
    as_account(client, "warehouse")
    client.post(
        "/api/v1/chat", json={"question": "材料是什麼？", "part_id": SECRET, "strategy": "mock"}
    )
    rows = _reads(client)
    assert len(rows) == before + 1
    assert rows[0]["what"] == "圖紙問答" and rows[0]["account_id"] == "warehouse"


def test_only_audit_viewers_see_the_reads(client):
    as_account(client, "warehouse")
    assert client.get("/api/v1/audit").status_code == 403
