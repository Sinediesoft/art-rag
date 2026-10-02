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


# 預設用主管測試：資料範圍是全部（docs/adr/012：訪客只能讀公開畫作），但沒有修改資料的權限。
# 測權限的測試自己切換身分
DEFAULT_ACCOUNT = "manager"


@pytest.fixture(scope="module", autouse=True)
def default_identity(client):
    """每個測試檔開始時切回預設身分，不受前一個檔案最後切到誰影響。"""
    as_account(client, DEFAULT_ACCOUNT)


def as_account(client, account_id: str) -> dict:
    """切換展示身分（工作階段 cookie 由 TestClient 保留）。"""
    r = client.post("/api/v1/auth/switch", json={"account_id": account_id})
    assert r.status_code == 200, r.text
    return r.json()["current"]
