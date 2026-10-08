"""單元測試一律用 mock 模型（共用層 §七）：不下載模型、不呼叫真模型或付費 API。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def mock_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    # DATABASE_URL 清空：.env 指向開發用資料庫時，下面的 build_index
    # 會把 mock 向量寫進去蓋掉真的索引。PostgreSQL 版另外用 TEST_DATABASE_URL 測（test_postgres.py）
    # 排程用簡易排程（不連 Timefold 服務）；記憶體管理關掉，避免測試時卸載開發者本機的模型
    # Jev 金鑰清空：開發者本機 .env 填了金鑰也不會真的呼叫 Jev（要測 Jev 的用 httpx.MockTransport）
    # 展示模式：程式預設關閉（docs/adr/030），測試要切換身分所以明確開啟，
    # 信任 TestClient 的主機名稱；評估模式維持關閉（要測的測試自己開）
    os.environ.update(
        {
            "EMBED_MODE": "mock",
            "LLM_MODE": "mock",
            "DATA_DIR": str(data),
            "DATABASE_URL": "",
            "SCHEDULER_MODE": "mock",
            "MEMORY_GUARD": "false",
            "JEV_API_KEY": "",
            "REARRANGE": "false",
            "DEMO_CONTROLS": "true",
            "DEMO_TRUSTED_HOSTS": '["testclient"]',
            "EVAL_CONTROLS": "false",
        }
    )
    from app.core import config

    config.get_settings.cache_clear()
    sys.path.insert(0, str(ROOT / "pipelines"))
    import build_index

    sys.argv = ["build_index.py"]
    assert build_index.main() == 0
    yield data


@pytest.fixture(scope="session")
def client(mock_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


# 預設用主管測試：資料範圍是全部（docs/adr/014：訪客只能讀公開畫作），但沒有修改資料的權限。
# 測權限的測試自己切換身分
DEFAULT_ACCOUNT = "manager"


@pytest.fixture(scope="module", autouse=True)
def default_identity(client):
    """每個測試檔開始時切回預設身分，不受前一個檔案最後切到誰影響。"""
    as_account(client, DEFAULT_ACCOUNT)


@pytest.fixture
def all_chunks(monkeypatch):
    """mock 向量是雜湊亂數，檢索只會留隨機 1 段；
    改成取這個對象（照 Metadata Filter）看得到的全部段落，第 4～6 段才看得到真的段落內容
    （地端相關性看關鍵詞，第 6 段看問題關鍵詞的覆蓋率）。"""
    from app.services import chat_service

    def every(question, artwork_id, part_id=None, scope=None):
        store = chat_service.get_store()
        coll, owner = (store.mfg, part_id) if part_id else (store.art, artwork_id)
        owners = chat_service.visible_parts(scope) if part_id else None
        qvec = chat_service.embed_text([question])[0]
        hits = coll.search_chunks(qvec, 99, owner, owners=owners)
        return [chat_service._source(i, h, store) for i, h in enumerate(hits)]

    monkeypatch.setattr(chat_service, "retrieve", every)


def as_account(client, account_id: str) -> dict:
    """切換展示身分（憑證 cookie 由 TestClient 保留）。切換要先有有效憑證（docs/adr/030），
    所以先取一次（沒有或失效時發訪客憑證）。"""
    client.get("/api/v1/auth/accounts").raise_for_status()
    r = client.post("/api/v1/auth/switch", json={"account_id": account_id})
    assert r.status_code == 200, r.text
    return r.json()["current"]
