"""展示還原（make demo-reset 會呼叫）：清掉圖紙頁開立的工單與所有排程結果。

執行中的後端會在下一次查詢時自動重建工廠資料庫，不必重啟。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.repositories.production_repo import get_production_repo


def main() -> int:
    repo = get_production_repo()
    n = len(repo.work_orders())
    repo.reset()
    print(f"✓ 已清除圖紙頁開立的 {n} 張工單與所有排程結果（{repo.path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
