"""舊的 data/production.sqlite3 搬進 PostgreSQL 的 production schema
（pipelines/import_factory_sqlite.py，docs/adr/048）。

用同一個測試用 PostgreSQL 裡另開的資料庫，不動其他測試共用的工廠資料。
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]

OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE work_orders (
  wo_no TEXT PRIMARY KEY, part_id TEXT NOT NULL, qty INTEGER NOT NULL, priority TEXT NOT NULL,
  release_on TEXT NOT NULL, due_on TEXT NOT NULL, note TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE data_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, change_no TEXT NOT NULL UNIQUE, op TEXT NOT NULL,
  params TEXT NOT NULL, summary TEXT NOT NULL, moved_on TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_label TEXT NOT NULL, approval_no TEXT, approver_label TEXT, created_at TEXT NOT NULL
);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_label TEXT NOT NULL, action TEXT NOT NULL, op TEXT, ref_no TEXT, summary TEXT,
  detail TEXT, request_id TEXT
);
"""


@pytest.fixture
def other_db(mock_env, monkeypatch):
    """同一台測試資料庫伺服器上另開一個空的資料庫，工廠資料暫時指向它。"""
    from app.core import config
    from app.repositories import factory_db

    base = os.environ["FACTORY_DATABASE_URL"]
    if not base:
        pytest.skip("沒有測試用的 PostgreSQL")
    name = "artrag_import_test"
    admin = make_conninfo(base, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name}")
        c.execute(f"CREATE DATABASE {name}")
    url = make_conninfo(base, dbname=name)
    factory_db.close_pool()
    monkeypatch.setenv("FACTORY_DATABASE_URL", url)
    config.get_settings.cache_clear()
    yield url
    factory_db.close_pool()
    monkeypatch.setenv("FACTORY_DATABASE_URL", base)
    config.get_settings.cache_clear()
    assert conninfo_to_dict(base)["dbname"] != name


def _old_file(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO meta VALUES ('revision', '7')")
    c.execute(
        "INSERT INTO work_orders VALUES ('WO-2611-01', 'mfg-001', 5, '一般', '2026-11-01',"
        " '2026-11-20', NULL, '已開立', '2026-11-01T00:00:00+00:00')"
    )
    c.execute(
        "INSERT INTO data_changes (change_no, op, params, summary, moved_on, actor_id, actor_label,"
        " created_at) VALUES ('IC-2611-01', 'stock_adjust', ?, '盤點', '2026-11-02', 'warehouse',"
        " '倉管', '2026-11-02T00:00:00+00:00')",
        (json.dumps({"part_id": "mfg-001", "warehouse_id": "WH-A", "delta": 1}),),
    )
    c.execute(
        "INSERT INTO audit_log (at, actor_id, actor_label, action, op, ref_no, detail) VALUES"
        " ('2026-11-02T00:00:00+00:00', 'warehouse', '倉管', '寫入', 'stock_adjust', 'IC-2611-01',"
        ' \'{"audit_id": "a-1"}\')'
    )
    c.execute(
        "INSERT INTO audit_log (at, actor_id, actor_label, action, detail) VALUES"
        " ('2026-11-02T00:00:01+00:00', 'manager', '主管', '拒絕', '一般文字')"
    )
    c.commit()
    c.close()


def test_import_moves_old_sqlite_into_production_schema(other_db, tmp_path):
    sys.path.insert(0, str(ROOT / "pipelines"))
    import import_factory_sqlite

    from app.repositories.production_repo import get_production_repo

    old = tmp_path / "production.sqlite3"
    _old_file(old)
    sys.argv = ["import_factory_sqlite.py", "--file", str(old)]
    assert import_factory_sqlite.main() == 0
    repo = get_production_repo()
    assert [w["wo_no"] for w in repo.work_orders()] == ["WO-2611-01"]
    assert repo.get_work_order("WO-2611-01")["created_by"] is None  # 舊檔沒有這一欄：留空
    assert [c["change_no"] for c in repo.changes()] == ["IC-2611-01"]
    assert int(repo.revision()) > 7  # 版本只往上加：庫存資料庫會重建
    # 稽核的 audit_id 從 detail 補回：同一個事件再送一次不會重複
    repo.add_audit(
        {"actor_id": "warehouse", "actor_label": "倉管", "action": "寫入"}, audit_id="a-1"
    )
    assert len(repo.audit(10)) == 2
    # 自動編號接在舊資料後面
    repo.add_change(
        {
            "change_no": "IC-2611-02", "op": "stock_adjust", "params": {}, "summary": "盤點",
            "moved_on": "2026-11-03", "actor_id": "warehouse", "actor_label": "倉管",
        }
    )  # fmt: skip
    assert [c["seq"] for c in repo.changes()] == [1, 2]
    # 已經有資料的 schema：不加 --replace 就不匯入
    assert import_factory_sqlite.main() == 1
    sys.argv += ["--replace"]
    assert import_factory_sqlite.main() == 0
    assert [c["change_no"] for c in repo.changes()] == ["IC-2611-01"]
