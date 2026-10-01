"""建工廠資料庫（make inventory）→ data/inventory.sqlite3。

來源：kb/inventory、kb/parts、kb/production，加上圖紙頁開立的工單與目前排程（production.sqlite3）。

後端偵測到 JSON 變動也會自動重建，這個指令用來手動重建或在 CI 驗證資料。

用法：
    python pipelines/build_inventory.py           # 驗證並重建
    python pipelines/build_inventory.py --check   # 只做 JSON Schema 與帳務一致性檢查（CI 用）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.rag.kb import validate_inventory, validate_parts, validate_production
from app.repositories.inventory_repo import get_inventory_repo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    parts, _ = validate_parts(check_drawing=False)
    site, items, errors = validate_inventory({p["id"] for p in parts})
    for e in errors:
        print(f"✗ {e}")
    if errors:
        return 1
    missing = sorted({p["id"] for p in parts} - {it["part_id"] for it in items})
    print(f"✓ 庫存資料 {len(items)} 張圖紙（資料日期 {site['as_of']}）")
    prod_site, routings, prod_errors = validate_production({p["id"] for p in parts}, items)
    for e in prod_errors:
        print(f"✗ {e}")
    if prod_errors:
        return 1
    print(
        f"✓ 生產排程：{len(prod_site['machines'])} 台機台、{len(routings)} 份製程途程"
        f"（排程起點 {prod_site['plan_start']}）"
    )
    if missing:
        print(f"  （{', '.join(missing)} 沒有庫存資料，只建零件主檔）")
    if args.check:
        return 0
    manifest = get_inventory_repo().rebuild()
    print(f"✓ 已重建 data/inventory.sqlite3：{manifest['tables']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
