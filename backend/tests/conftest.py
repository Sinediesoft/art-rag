"""單元測試一律用 mock 模型（共用層 §七）：不下載模型、不呼叫真模型或付費 API。"""

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# 背景工作的隔離環境（PostgreSQL＋Redis＋假 Timefold＋Celery worker）：fixture 在 jobs_support.py
pytest_plugins = ["jobs_support"]


@pytest.fixture(scope="session", autouse=True)
def mock_env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    # DATABASE_URL 清空：.env 指向開發用資料庫時，下面的 build_index
    # 會把 mock 向量寫進去蓋掉真的索引。PostgreSQL 版另外用 TEST_DATABASE_URL 測（test_postgres.py）
    # 排程用簡易排程（不連 Timefold 服務）；記憶體管理關掉，避免測試時卸載開發者本機的模型
    # Jev 金鑰清空：開發者本機 .env 填了金鑰也不會真的呼叫 Jev（要測 Jev 的用 httpx.MockTransport）
    # 展示模式：程式預設關閉（docs/adr/030），測試要切換身分所以明確開啟，
    # 信任 TestClient 的來源（testclient）與 Host（testserver）；
    # 評估模式維持關閉（要測的測試自己開）
    # JWT_SECRET 清空：開發者本機 .env 填了太短的金鑰時，後端會拒絕啟動，整套測試都跑不了
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
            "DEMO_TRUSTED_HOST_NAMES": '["testserver"]',
            "EVAL_CONTROLS": "false",
            "JWT_SECRET": "",
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


# 預設用主管測試：資料範圍是全部（docs/adr/014：一般員工只能讀公開畫作），
# 也有所有操作（docs/adr/036）。
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
    所以先取一次（沒有或失效時發一般員工的憑證）。"""
    client.get("/api/v1/auth/accounts").raise_for_status()
    r = client.post("/api/v1/auth/switch", json={"account_id": account_id})
    assert r.status_code == 200, r.text
    return r.json()["current"]


# 測試專用帳號（D2-A，TASK-20261014-001）：docs/adr/036 之後只剩 5 個展示帳號、一個主管，
# 要表達「同權限、但不是擁有者」的測試（別的主管看不到、取消不了我的排程工作；照片分享給另一個
# 倉管…）用這兩個。只疊加在測試的 shared/ 副本，不寫進 shared/access.yaml、不出現在頁首。
TEST_ACCOUNTS = {
    "manager_b": {
        "label": "主管・乙（測試）", "role": "manager",
        "warehouses": ["*"], "customers": ["*"], "artworks": ["*"],
    },
    "warehouse_b": {"label": "倉管・乙（測試）", "role": "warehouse", "warehouses": ["*"]},
}  # fmt: skip


@pytest.fixture
def extra_accounts(tmp_path):
    """shared/ 的副本加上 TEST_ACCOUNTS，API 與之後啟動的背景 worker（繼承環境變數）都改看它。"""
    from app.core import config

    dst = tmp_path / "shared_extra"
    shutil.copytree(config.get_settings().shared_dir, dst)
    path = dst / "access.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["accounts"].update(TEST_ACCOUNTS)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    old = os.environ.get("ARTRAG_SHARED_DIR")
    os.environ["ARTRAG_SHARED_DIR"] = str(dst)
    config.get_settings.cache_clear()
    try:
        yield dst
    finally:
        if old is None:
            os.environ.pop("ARTRAG_SHARED_DIR", None)
        else:
            os.environ["ARTRAG_SHARED_DIR"] = old
        config.get_settings.cache_clear()
