"""單元測試一律用 mock 模型（共用層 §七）：不下載模型、不呼叫真模型或付費 API。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def mock_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    # 排程用簡易排程（不連 Timefold 服務）；記憶體管理關掉，避免測試時卸載開發者本機的模型
    os.environ.update(
        {
            "EMBED_MODE": "mock",
            "LLM_MODE": "mock",
            "DATA_DIR": str(data),
            "SCHEDULER_MODE": "mock",
            "MEMORY_GUARD": "false",
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


def as_account(client, account_id: str) -> dict:
    """切換展示身分（工作階段 cookie 由 TestClient 保留）。"""
    r = client.post("/api/v1/auth/switch", json={"account_id": account_id})
    assert r.status_code == 200, r.text
    return r.json()["current"]
