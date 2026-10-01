"""展示還原（make demo-reset 會呼叫）：清掉圖紙頁開立的工單、所有排程結果，
以及智慧助理寫入的異動、待核准單與稽核紀錄。

執行中的後端會在下一次查詢時自動重建工廠資料庫，不必重啟。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.repositories.production_repo import get_production_repo


def main() -> int:
    repo = get_production_repo()
    n = len(repo.work_orders())
    changes = len(repo.changes())
    approvals = len(repo.approvals(limit=10_000))
    repo.reset()
    print(
        f"✓ 已清除開立的 {n} 張工單、所有排程結果、{changes} 筆異動與 {approvals} 張核准單"
        f"（{repo.path}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
