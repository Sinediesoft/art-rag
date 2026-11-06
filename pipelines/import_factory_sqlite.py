"""把舊的 data/production.sqlite3（工單、排程、異動、核准、稽核、冪等）
搬進 PostgreSQL 的 production schema（docs/adr/048）。
庫存資料庫（inventory.sqlite3）不用搬：後端會從知識庫＋production 自動重建。

只搬到「還沒有任何生產資料」的資料庫（避免和已經在 PostgreSQL 寫入的資料混在一起）；
要覆蓋就加 --replace（先清空 production schema 再匯入）。搬完的舊檔不刪，確認沒問題再自己刪。

用法（要先設定 DATABASE_URL 或 FACTORY_DATABASE_URL）：
    python pipelines/import_factory_sqlite.py                  # 讀 data/production.sqlite3
    python pipelines/import_factory_sqlite.py --file 路徑       # 指定舊檔
    python pipelines/import_factory_sqlite.py --replace        # 先清空 production schema
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.repositories import factory_db
from app.repositories.production_repo import TABLES, ensure_schema


def _columns(pg, table: str) -> list[str]:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'production' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r["column_name"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=get_settings().data_dir / "production.sqlite3")
    ap.add_argument("--replace", action="store_true", help="先清空 production schema 再匯入")
    args = ap.parse_args()
    if not args.file.is_file():
        print(f"✗ 找不到 {args.file}（沒有舊的生產資料就不用搬）")
        return 1
    try:
        target = factory_db.describe(factory_db.url())
    except factory_db.FactoryUnavailable as e:
        print(f"✗ {e.message}")
        return 1
    ensure_schema()
    src = sqlite3.connect(f"{args.file.resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    have = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts: dict[str, int] = {}
    with factory_db.connection() as pg:
        factory_db.xact_lock(pg, factory_db.PRODUCTION_WRITE)
        existing = sum(
            pg.execute(f"SELECT count(*) AS n FROM production.{t}").fetchone()["n"] for t in TABLES
        )
        if existing and not args.replace:
            print(f"✗ {target} 的 production schema 已經有 {existing} 筆資料；要覆蓋請加 --replace")
            return 1
        if args.replace:
            pg.execute(
                "TRUNCATE " + ", ".join(f"production.{t}" for t in TABLES) + " RESTART IDENTITY"
            )
        for table in TABLES:
            if table not in have:
                continue
            cols = [c for c in _columns(pg, table) if c != "audit_id"]
            src_cols = {r["name"] for r in src.execute(f"PRAGMA table_info({table})")}
            cols = [
                c for c in cols if c in src_cols
            ]  # 舊版檔案缺少的欄位（score_check、created_by）留空
            rows = [tuple(r[c] for c in cols) for r in src.execute(f"SELECT * FROM {table}")]
            if rows:
                marks = ", ".join(["%s"] * len(cols))
                with pg.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO production.{table} ({', '.join(cols)}) VALUES ({marks})", rows
                    )
            counts[table] = len(rows)
        # 自動編號接在舊資料後面；稽核的 audit_id 從 detail 補回（去重用）
        for table, col in (("data_changes", "seq"), ("audit_log", "id")):
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('production.{table}', '{col}'),"
                f" COALESCE((SELECT max({col}) FROM production.{table}), 0) + 1, false)"
            )
        # CASE 保證先判斷是不是 JSON 物件才轉型（一般文字的 detail 不會轉型失敗）
        pg.execute(
            "UPDATE production.audit_log SET audit_id = detail::jsonb->>'audit_id'"
            " WHERE audit_id IS NULL AND CASE WHEN left(detail, 1) = '{'"
            " THEN detail::jsonb ? 'audit_id' ELSE false END"
        )
        revision = src.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        old = int(revision["value"]) if revision else 0
        # 版本只往上加：庫存資料庫看到版本變了就會重建
        pg.execute(
            "UPDATE production.meta SET value = (GREATEST(value::bigint, %s) + 1)::text"
            " WHERE key = 'revision'",
            (old,),
        )
    src.close()
    print(f"✓ 已從 {args.file} 匯入 {target} 的 production schema：{counts}")
    print("  庫存資料庫會在後端下一次查詢時自動重建；舊檔沒有刪除，確認沒問題後可以自己刪掉")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
