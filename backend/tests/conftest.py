"""單元測試一律用 mock 模型（共用層 §七）：不下載模型、不呼叫真模型或付費 API。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def mock_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    os.environ.update({"EMBED_MODE": "mock", "LLM_MODE": "mock", "DATA_DIR": str(data)})
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
