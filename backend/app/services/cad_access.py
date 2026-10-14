"""3D 重建工作的存取紀錄（TASK-20261013-001，docs/adr/041）。

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


def attempt_dir(job_id: str, attempt: int) -> Path:
    """背景工作某一次嘗試自己的產物資料夾（job_dir/a<attempt>）。每次嘗試只發布到自己的資料夾，
    不刪、不換別的嘗試的檔案；租約已失效的舊嘗試就算晚到發布，也碰不到新嘗試的產物。"""
    return job_dir(job_id) / f"a{int(attempt)}"


# 背景工作結束時（fenced 的 store.finish）寫進結果的發布紀錄：哪一次嘗試的資料夾是正式產物
PUBLISHED_KEY = "published_attempt"


def published_attempt(row: dict) -> int | None:
    """背景工作哪一次嘗試的產物可讀。只認 fenced 的 store.finish() 寫進結果的發布紀錄，
    而不是只看狀態：恢復迴圈判定的失敗（LEASE_EXPIRED，從沒通過 finish）、取消、還在執行的工作
    都沒有這個紀錄——租約失效後晚到的舊嘗試就算把資料夾發布到磁碟上，也不會被任何入口讀到。
    正常結束的成功與執行失敗（CAD_EXEC_FAILED，有可檢視的產物）都有。"""
    if row.get("status") not in ("succeeded", "failed"):
        return None
    result = row.get("result")
    n = result.get(PUBLISHED_KEY) if isinstance(result, dict) else None
    if isinstance(n, int) and not isinstance(n, bool) and n == row.get("attempt"):
        return n
    return None


def published_dir(job_id: str) -> Path | None:
    """目前可讀的產物資料夾。背景工作（工作資料庫有這一列）：發布紀錄指定的那一次嘗試的資料夾
    （見 published_attempt）；沒有發布紀錄回 None。新舊入口（/jobs/{id}/artifacts、/cad/jobs/…、
    重建紀錄）都經過這裡，對「產物是否已發布」的答案一致。
    舊 SSE 入口的工作（資料庫沒有這一列）：工作資料夾本身。"""
    if _job_lookup is not None and (row := _job_lookup(job_id)) is not None:
        n = published_attempt(row)
        return attempt_dir(job_id, n) if n is not None else None
    return job_dir(job_id)


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
