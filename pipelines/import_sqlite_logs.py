"""把 SQLite（data/artrag.sqlite3）裡的使用紀錄搬進 PostgreSQL（.env 的 DATABASE_URL）。

改用資料庫之前的問答紀錄、上傳紀錄、3D 重建紀錄都在 SQLite 裡；改用 PostgreSQL 後後端改讀資料庫，
這些就看不到了（例如圖紙頁「最近的 3D 重建」變空）。
搬一次就好，重複執行也不會重複寫入；SQLite 檔不會被修改。

用法：
    python pipelines/import_sqlite_logs.py                       # 預設讀 data/artrag.sqlite3
    python pipelines/import_sqlite_logs.py --sqlite 別的檔.sqlite3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.repositories.db import DatabaseUnavailable, describe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", type=Path, help="SQLite 檔（預設 data/artrag.sqlite3）")
    args = parser.parse_args()

    s = get_settings()
    if not s.database_url:
        print("DATABASE_URL 沒有設定：請先在 .env 填入資料庫位址（見 .env.example）")
        return 1
    path = args.sqlite or s.data_dir / "artrag.sqlite3"
    if not path.exists():
        print(f"找不到 {path}，沒有要搬的紀錄")
        return 1

    from app.repositories.pg_logs_repo import PgLogsRepo

    try:
        counts = PgLogsRepo().import_sqlite(path)
    except DatabaseUnavailable as e:
        print(e)
        return 1
    print(f"{path} → PostgreSQL {describe(s.database_url)}")
    for table, (total, added) in counts.items():
        print(f"  {table}：SQLite {total} 筆，新寫入 {added} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
