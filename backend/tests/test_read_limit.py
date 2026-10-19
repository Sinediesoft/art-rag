"""機密檔案的讀取頻率限制：同一個帳號短時間內下載機密圖紙、3D 模型太多次就回 429 並記進安全紀錄。
內部人員演練：倉管 0.4 秒內下載了 6 張機密圖紙與 STEP／STL 模型，沒有任何限制。"""

import pytest
from conftest import as_account

from app.core import config

SECRET, INTERNAL = "mfg-001", "mfg-004"  # 機密、內部


@pytest.fixture
def limit_3(monkeypatch):
    # 預設行程內計數：.env 設了 REDIS_URL 時不碰正在用的 Redis（要測 Redis 的另外接 local_redis）
    from app.jobs import coordination

    monkeypatch.setattr(config.get_settings(), "redis_url", "")
    monkeypatch.setattr(coordination, "_client", None)
    cfg = config.get_access_config()
    monkeypatch.setitem(
        cfg, "read_limits", {"levels": ["機密"], "window_s": 600, "max_downloads": 3}
    )


def _limit_rows(client) -> list[dict]:
    as_account(client, "manager")
    items = client.get("/api/v1/security/logs").json()["items"]
    return [x for x in items if x["rule"] == "機密檔案下載過多"]


def test_secret_downloads_over_the_limit_are_refused_and_logged(client, limit_3):
    before = len(_limit_rows(client))
    as_account(client, "warehouse")
    urls = [
        f"/api/v1/parts/{SECRET}/drawing",
        f"/api/v1/parts/{SECRET}/model.step",
        f"/api/v1/parts/{SECRET}/model.stl",
    ]
    assert [client.get(u).status_code for u in urls] == [200, 200, 200]
    r = client.get(f"/api/v1/parts/{SECRET}/drawing")
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert "倉管" not in r.json()["error"]["message"]  # 訊息不重複身分，只說次數
    # 主管在安全紀錄看得到是誰、要哪張（只記一筆：第 4 次）
    rows = _limit_rows(client)
    assert len(rows) == before + 1
    assert rows[0]["account_id"] == "warehouse" and SECRET in rows[0]["text"]


def test_thumbnails_internal_parts_and_other_accounts_are_not_counted(client, limit_3):
    as_account(client, "warehouse")
    for _ in range(5):  # 縮圖、內部等級的圖紙不算
        assert client.get(f"/api/v1/parts/{SECRET}/drawing?size=thumb").status_code == 200
        assert client.get(f"/api/v1/parts/{INTERNAL}/drawing").status_code == 200
    for _ in range(3):
        assert client.get(f"/api/v1/parts/{SECRET}/drawing").status_code == 200
    assert client.get(f"/api/v1/parts/{SECRET}/drawing").status_code == 429
    as_account(client, "manager")  # 各帳號分開算
    assert client.get(f"/api/v1/parts/{SECRET}/drawing").status_code == 200


def test_refused_download_is_not_counted_and_window_expires(client, limit_3, monkeypatch):
    from app.services import read_limit

    clock = [1000.0]
    monkeypatch.setattr(read_limit.time, "monotonic", lambda: clock[0])
    as_account(client, "manager")  # 主管也算
    for _ in range(3):
        assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 200
    for _ in range(3):  # 被拒的那幾次不再累加
        assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 429
    clock[0] += 601  # 超過 10 分鐘：重新計算
    assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 200


def test_no_permission_still_403_not_429(client, limit_3):
    """看不到的圖紙照舊 403（不洩漏存在），也不算進次數"""
    as_account(client, "sales")
    for _ in range(5):
        assert client.get(f"/api/v1/parts/{SECRET}/drawing").status_code == 403


@pytest.fixture
def local_redis(monkeypatch):
    """本機有 Redis（make jobs-up）才測；用第 15 號資料庫，不碰正在用的資料"""
    import redis

    from app.jobs import coordination

    url = "redis://127.0.0.1:6379/15"
    try:
        redis.Redis.from_url(url, socket_connect_timeout=0.5).ping()
    except redis.RedisError:
        pytest.skip("本機沒有 Redis")
    monkeypatch.setattr(config.get_settings(), "redis_url", url)
    monkeypatch.setattr(coordination, "_client", None)
    from app.services import read_limit

    read_limit.reset()
    yield read_limit
    read_limit.reset()


def test_counts_are_shared_through_redis(client, limit_3, local_redis):
    """多個 API 行程共用 Redis 的計數：清掉這個行程的記憶體（模擬另一個行程）照樣被擋"""
    as_account(client, "warehouse")
    for _ in range(2):
        assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 200
    local_redis._hits.clear()  # 另一個行程沒有這些紀錄
    assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 200
    local_redis._hits.clear()
    assert client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code == 429


def test_falls_back_to_local_counting_when_redis_is_down(client, limit_3, monkeypatch):
    """Redis 不通：改在行程內計數，限制照樣有"""
    from app.jobs import coordination

    monkeypatch.setattr(config.get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(coordination, "_client", None)
    as_account(client, "warehouse")
    codes = [client.get(f"/api/v1/parts/{SECRET}/model.stl").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
