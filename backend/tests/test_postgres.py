"""PostgreSQL + pgvector 版的存取層：結果要與檔案版／SQLite 版一致（docs/adr/009）。

設了 TEST_DATABASE_URL 才跑（CI 用 pgvector 容器）。那個資料庫要可以清空：
測試會刪掉重建索引表、清空使用紀錄，不要指向開發用的 artrag 資料庫。
"""

import os
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="沒有設 TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def pool():
    from app.repositories.db import open_pool

    p = open_pool(URL)
    yield p
    p.close()


@pytest.fixture(scope="module")
def stores(mock_env, pool):
    from app.core.config import get_settings
    from app.repositories.index_store import IndexStore
    from app.repositories.pg_index_store import PgIndexStore, publish_index

    index_dir = get_settings().index_dir
    publish_index(index_dir, pool)
    files, pg = IndexStore(index_dir), PgIndexStore(index_dir, pool)
    files.load()
    pg.load()
    return files, pg


def _ids(hits, key):
    return [h.item[key] for h in hits]


def test_snapshot_matches_files(stores):
    files, pg = stores
    assert pg.manifest == files.manifest
    for a, b in ((files.art, pg.art), (files.mfg, pg.mfg)):
        assert a.items and b.items == a.items  # 順序也要一樣：以文搜圖同分時照知識庫順序
        assert b.chunks == a.chunks
        np.testing.assert_allclose(b.image_vecs, a.image_vecs, atol=1e-6)
        np.testing.assert_allclose(b.chunk_vecs, a.chunk_vecs, atol=1e-6)


@pytest.mark.parametrize("domain", ["art", "mfg"])
def test_vector_search_matches_files(stores, domain):
    files, pg = stores
    a, b = getattr(files, domain), getattr(pg, domain)
    rng = np.random.default_rng(0)
    probe = rng.standard_normal(a.image_vecs.shape[1]).astype(np.float32)
    for q in (a.image_vecs[0], probe / np.linalg.norm(probe)):
        ha, hb = a.search_images(q, 3), b.search_images(q, 3)
        assert _ids(hb, "id") == _ids(ha, "id")
        np.testing.assert_allclose([h.score for h in hb], [h.score for h in ha], atol=1e-5)

    q, first = a.chunk_vecs[0], a.chunks[0]
    owner = first[a.owner_key]
    for kwargs in ({}, {"owner_id": owner}, {"owner_id": owner, "exclude": {first["chunk_id"]}}):
        ha, hb = a.search_chunks(q, 4, **kwargs), b.search_chunks(q, 4, **kwargs)
        assert _ids(hb, "chunk_id") == _ids(ha, "chunk_id"), kwargs
        np.testing.assert_allclose([h.score for h in hb], [h.score for h in ha], atol=1e-5)
    only_owner = b.search_chunks(q, 50, owner_id=owner)
    assert {h.item[a.owner_key] for h in only_owner} == {owner}


def test_reload_after_republish(stores, pool):
    from app.repositories.pg_index_store import publish_index

    files, pg = stores
    assert not pg.maybe_reload()
    publish_index(files.dir, pool)
    assert pg.maybe_reload()  # make index 後，執行中的後端不必重啟
    assert not pg.maybe_reload()


def test_empty_database_refuses_to_load(stores, pool):
    from app.repositories.index_store import IndexMismatch
    from app.repositories.pg_index_store import PgIndexStore, publish_index

    files, _ = stores
    with pool.connection() as conn:
        conn.execute("DROP TABLE index_manifest")
    try:
        with pytest.raises(IndexMismatch, match="make index"):
            PgIndexStore(files.dir, pool).load()
    finally:
        publish_index(files.dir, pool)


def test_logs_roundtrip(pool):
    from app.repositories.pg_logs_repo import PgLogsRepo

    repo = PgLogsRepo(pool)
    with pool.connection() as conn:
        conn.execute("TRUNCATE uploads, chat_logs, cad_logs, sql_logs, feedback")
    assert repo.ping()

    repo.add_upload("img-new", "img-new.jpg")
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    repo._exec("INSERT INTO uploads VALUES (?, ?, ?)", ("img-old", "img-old.jpg", old))
    assert repo.get_upload("img-new") == "img-new.jpg"
    assert repo.purge_uploads(7) == ["img-old.jpg"]  # 照片 7 天後刪除
    assert repo.get_upload("img-old") is None

    created = repo.now()
    repo.add_chat_log(
        {
            "request_id": "req-1",
            "created_at": created,
            "artwork_id": "npm-000001",
            "part_id": None,
            "strategy_used": "hybrid",
            "use_retrieval": 1,
            "fallback": 0,
            "cost_twd": 0.0,
            "egress_bytes": 0,
        }
    )
    (chat,) = repo.recent_chats(5)
    assert chat["request_id"] == "req-1" and chat["fallback"] == 0
    assert chat["created_at"] == created  # 讀回來仍是 ISO 8601（UTC）字串，與 SQLite 版相同

    repo.add_cad_log(
        {"request_id": "req-2", "created_at": repo.now(), "job_id": "job-1", "part_id": "mfg-001"}
    )
    assert repo.recent_cad_for_part("mfg-001")[0]["job_id"] == "job-1"
    assert repo.recent_cad(5)[0]["request_id"] == "req-2"
    repo.add_sql_log(
        {"request_id": "req-3", "created_at": repo.now(), "sql": "SELECT 1", "ok": 1, "attempts": 1}
    )
    (sql,) = repo.recent_sql(5)  # 庫存 Text-to-SQL 的紀錄（系統狀態頁）
    assert sql["request_id"] == "req-3" and sql["sql"] == "SELECT 1"
    repo.add_feedback("req-1", "up", None)


def test_import_sqlite_is_idempotent(pool, tmp_path):
    from app.repositories.logs_repo import LogsRepo
    from app.repositories.pg_logs_repo import PgLogsRepo

    path = tmp_path / "old.sqlite3"
    old = LogsRepo(path)
    old.add_upload("img-a", "img-a.jpg")
    created = old.now()
    old.add_chat_log({"request_id": "req-a", "created_at": created, "use_retrieval": 1})
    old.add_cad_log(
        {
            "request_id": "req-b",
            "created_at": old.now(),
            "job_id": "job-b",
            "part_id": "mfg-002",
            "iou": 0.81,
        }
    )
    old.add_sql_log({"request_id": "req-c", "created_at": old.now(), "sql": "SELECT 1", "ok": 1})
    old.add_feedback("req-a", "up", "答得好")
    old._conn.close()

    repo = PgLogsRepo(pool)
    with pool.connection() as conn:
        conn.execute("TRUNCATE uploads, chat_logs, cad_logs, sql_logs, feedback")
    assert repo.import_sqlite(path) == {
        "uploads": (1, 1),
        "chat_logs": (1, 1),
        "cad_logs": (1, 1),
        "sql_logs": (1, 1),
        "feedback": (1, 1),
    }
    # 再搬一次不會重複寫入（feedback 沒有主鍵，也要擋得住）
    assert all(added == 0 for _, added in repo.import_sqlite(path).values())
    (cad,) = repo.recent_cad_for_part("mfg-002")  # 圖紙頁「最近的 3D 重建」看得到
    assert cad["job_id"] == "job-b" and cad["iou"] == 0.81
    assert repo.recent_chats(5)[0]["created_at"] == created  # 時間原樣保留
