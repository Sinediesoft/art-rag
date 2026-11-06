"""展示還原（make demo-reset 會呼叫）：清掉圖紙頁開立的工單、所有排程結果，
智慧助理寫入的異動、待核准單與稽核紀錄，以及五段防護的攔截紀錄（docs/adr/014）。

執行中的後端會在下一次查詢時自動重建庫存資料庫，不必重啟。生產資料在 PostgreSQL 的 production schema
（docs/adr/048），要先設定 DATABASE_URL（或 FACTORY_DATABASE_URL）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.repositories import factory_db
from app.repositories.logs_repo import get_logs_repo
from app.repositories.production_repo import get_production_repo


def main() -> int:
    repo = get_production_repo()
    n = len(repo.work_orders())
    changes = len(repo.changes())
    approvals = len(repo.approvals(limit=10_000))
    repo.reset()
    blocked = get_logs_repo().clear_security()
    print(
        f"✓ 已清除開立的 {n} 張工單、所有排程結果、{changes} 筆異動與 {approvals} 張核准單"
        f"（{factory_db.describe(factory_db.url())} 的 production schema），"
        f"以及 {blocked} 筆攔截紀錄"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
