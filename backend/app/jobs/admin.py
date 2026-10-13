"""背景工作系統的管理指令（部署、遷移、恢復）：

    python -m app.jobs.admin migrate                 # 套用 migration（可重複執行）
    python -m app.jobs.admin status                  # migration 與工作狀態
    python -m app.jobs.admin rollback --steps 1      # 回復最近 N 個 migration（先備份！）
    python -m app.jobs.admin maintain                # 持續跑 outbox 投遞與租約恢復
    python -m app.jobs.admin recover                 # 手動跑一輪恢復與投遞
    python -m app.jobs.admin adopt-uploads --owner manager   # 舊上傳（沒有擁有者）指派給某帳號
    python -m app.jobs.admin adopt-cad --owner manager       # 舊 3D 重建資料夾補上擁有者
    python -m app.jobs.admin purge                   # 刪除超過保存期限的工作與產物

只動背景工作系統自己的資料表與 data/cad、uploads 的存取紀錄；不碰知識庫、庫存與生產資料庫。
"""

import argparse
import json
import sys
import time

from app.core.config import get_settings
from app.jobs import boot, broker, db, migrate
from app.services import cad_access, identity
from app.services.security import CadAccess


def _owner(account_id: str) -> str:
    identity.get_account(account_id)  # 帳號不存在就停
    return account_id


def cmd_adopt_cad(owner: str) -> int:
    """data/cad 裡沒有 access.json 的舊資料夾：知識庫圖紙來源的本來就依圖紙規則可讀，
    這裡把（有摘要而且是照片來源的）指派給 owner，讓它之後可讀。"""
    root = get_settings().cad_jobs_dir
    n = 0
    for d in sorted(root.glob("cad_*")) if root.is_dir() else []:
        if (d / cad_access.ACCESS_FILE).is_file() or not (d / "summary.json").is_file():
            continue
        meta = (json.loads((d / "summary.json").read_text(encoding="utf-8")).get("meta")) or {}
        part = meta.get("part") or {}
        cad_access.write_access(
            d.name,
            CadAccess(d.name, owner, None if meta.get("image_id") else part.get("id"),
                      meta.get("image_id"), part.get("id") if meta.get("image_id") else None),
            d,
        )  # fmt: skip
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.jobs.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")
    rb = sub.add_parser("rollback")
    rb.add_argument("--steps", type=int, default=1)
    sub.add_parser("maintain")
    sub.add_parser("recover")
    sub.add_parser("purge")
    for name in ("adopt-uploads", "adopt-cad"):
        a = sub.add_parser(name)
        a.add_argument("--owner", required=True)
    args = p.parse_args(argv)
    s = get_settings()

    try:
        if args.cmd == "migrate":
            done = migrate.apply()
            print("已套用：" + ("、".join(done) if done else "（沒有新的 migration）"))
        elif args.cmd == "status":
            print(json.dumps({"migrations": migrate.status(), "jobs": boot.status()},
                             ensure_ascii=False, indent=2))  # fmt: skip
        elif args.cmd == "rollback":
            print("已回復：" + "、".join(migrate.rollback(args.steps)))
        elif args.cmd == "maintain":
            print("維護迴圈啟動（Ctrl-C 結束）")
            while True:
                broker.maintenance_once()
                time.sleep(s.jobs_dispatch_interval_s)
        elif args.cmd == "recover":
            print(json.dumps(broker.maintenance_once(stall_s=0), ensure_ascii=False))
        elif args.cmd == "purge":
            print(f"已刪除 {boot.purge(s.jobs_event_retention_days)} 個工作")
        elif args.cmd == "adopt-uploads":
            from app.repositories.logs_repo import get_logs_repo

            print(f"已指派 {get_logs_repo().adopt_uploads(_owner(args.owner))} 張舊上傳")
        elif args.cmd == "adopt-cad":
            print(f"已補上 {cmd_adopt_cad(_owner(args.owner))} 個舊 3D 重建資料夾的存取紀錄")
    except (db.JobsUnavailable, migrate.MigrationError) as e:
        print(f"失敗：{getattr(e, 'message', e)}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
