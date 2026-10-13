"""3D 重建工作的存取紀錄（TASK-20261013-001，docs/adr/036）。

每個工作從建立的第一刻起就有擁有者、來源資源與可見範圍，授權不看 `summary.json` 在不在：
未完成、失敗、已完成的狀態、事件、摘要、程式碼、輸入圖、預覽與模型都走 `security.cad_can_read`。

存取紀錄的來源（依序）：
1. 背景工作資料庫的 jobs 列（新的 `POST /cad/jobs`）
2. 工作資料夾裡的 `access.json`（既有的 SSE 入口 `POST /cad/reconstruct`，建立資料夾後第一個寫入）
3. 舊資料（沒有 access.json）：從 `summary.json` 推導，只有知識庫圖紙來源的可以讀；
   照片來源的因為不知道擁有者而拒絕，需要管理者遷移（`python -m app.jobs.admin adopt-cad`）
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.services.security import CadAccess

JOB_ID = re.compile(r"^cad_[0-9a-f]{16}$")
ACCESS_FILE = "access.json"
JOB_FILES = {"model.stl", "model.step", "reproj.png", "code.py", "input.png"}

# 背景工作資料庫的查詢掛在這裡（app.jobs.store 設定後才有；沒有工作系統時為 None）
_job_lookup = None


def set_job_lookup(fn) -> None:
    global _job_lookup
    _job_lookup = fn


def job_dir(job_id: str) -> Path:
    return get_settings().cad_jobs_dir / job_id


def write_access(job_id: str, access: CadAccess, directory: Path | None = None) -> None:
    d = directory or job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    body = {**access.to_dict(), "job_id": job_id, "created_at": datetime.now(UTC).isoformat()}
    tmp = d / (ACCESS_FILE + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / ACCESS_FILE)


def update_access(job_id: str, directory: Path | None = None, **changes) -> None:
    """工作過程中得知的資訊（例如照片辨識出的知識庫圖紙）補進存取紀錄。"""
    d = directory or job_dir(job_id)
    path = d / ACCESS_FILE
    body = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"job_id": job_id}
    body.update({k: v for k, v in changes.items() if v is not None})
    tmp = d / (ACCESS_FILE + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_access(job_id: str) -> CadAccess | None:
    if not JOB_ID.match(job_id):
        return None
    if _job_lookup is not None and (row := _job_lookup(job_id)) is not None:
        return CadAccess.from_dict(job_id, row["resource"] | {"owner_id": row["owner_id"]})
    d = job_dir(job_id)
    path = d / ACCESS_FILE
    if path.is_file():
        return CadAccess.from_dict(job_id, json.loads(path.read_text(encoding="utf-8")))
    summary = d / "summary.json"
    if summary.is_file():
        meta = (json.loads(summary.read_text(encoding="utf-8")).get("meta")) or {}
        part = meta.get("part") or {}
        return CadAccess.from_dict(
            job_id,
            {"owner_id": None, "part_id": part.get("id"), "image_id": meta.get("image_id")},
            legacy=True,
        )
    return None
