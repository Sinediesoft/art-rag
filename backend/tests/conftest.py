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
    os.environ.update(
        {"EMBED_MODE": "mock", "LLM_MODE": "mock", "DATA_DIR": str(data), "DATABASE_URL": ""}
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
