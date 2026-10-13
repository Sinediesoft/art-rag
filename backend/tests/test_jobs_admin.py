"""背景工作的管理指令（部署、遷移、恢復）與舊資料的管理者遷移流程（TASK-20261013-001，AC-06、AC-20）。"""

import json
import shutil

from conftest import as_account

from app.core.config import REPO_ROOT, get_settings
from app.jobs import admin, db, migrate, store
from app.repositories.logs_repo import get_logs_repo
from app.services import cad_access


def run(capsys, *argv) -> tuple[int, str]:
    code = admin.main(list(argv))
    db.close()  # 指令結束會關連線池；測試其他部分之後會再開
    return code, capsys.readouterr().out


def test_migrate_status_and_rollback_commands(jobs, capsys):
    code, out = run(capsys, "migrate")
    assert code == 0 and "沒有新的 migration" in out  # 測試環境已套用
    code, out = run(capsys, "status")
    status = json.loads(out)
    assert status["migrations"][0] == {
        "version": "0001",
        "name": "0001_jobs.up.sql",
        "applied": True,
        "changed": False,
    }
    assert status["jobs"]["enabled"] and status["jobs"]["database_ok"]
    code, out = run(capsys, "rollback", "--steps", "1")
    assert code == 0 and "0001_jobs.down.sql" in out
    assert [m["applied"] for m in migrate.status()] == [False]
    code, out = run(capsys, "migrate")
    assert code == 0 and "0001_jobs.up.sql" in out and migrate.status()[0]["applied"]


def test_recover_command_requeues_expired_jobs(jobs, capsys):
    job, _ = store.create_job(
        kind="cad", owner_id="planner", params={}, resource={"part_id": "mfg-001"},
        scope="cad:mfg-001", request_id="r",
    )  # fmt: skip
    store.claim(job["id"], "w1")
    jobs.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job["id"],)
    )
    code, out = run(capsys, "recover")
    assert code == 0 and job["id"] in out and store.get(job["id"])["status"] == "queued"


def test_adopt_uploads_gives_legacy_uploads_to_one_account(jobs, client, capsys):
    repo = get_logs_repo()
    get_settings().uploads_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "kb/drawings/mfg-001.png", get_settings().uploads_dir / "adopt1.png")
    repo._exec(
        "INSERT INTO uploads (image_id, path, created_at, owner_id, acl)"
        " VALUES (?, ?, ?, NULL, NULL)",
        ("img_adopt0000000001", "adopt1.png", repo.now()),
    )
    as_account(client, "planner")
    assert client.get("/api/v1/images/img_adopt0000000001").status_code == 404
    code, out = run(capsys, "adopt-uploads", "--owner", "planner")
    assert code == 0 and "已指派" in out
    assert client.get("/api/v1/images/img_adopt0000000001").status_code == 200
    as_account(client, "manager")
    assert client.get("/api/v1/images/img_adopt0000000001").status_code == 404
    # 帳號不存在就停下來，不會指派給不存在的人
    try:
        admin.main(["adopt-uploads", "--owner", "nobody"])
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    assert raised
    db.close()


def test_adopt_cad_adds_access_records_to_legacy_photo_folders(jobs, client, capsys):
    d = get_settings().cad_jobs_dir / "cad_a0000000000000ad"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "input.png").write_bytes(b"x")
    meta = {"part": {"id": "mfg-001"}, "image_id": "img_old0000000000001"}
    (d / "summary.json").write_text(json.dumps({"meta": meta, "result": {}, "done": {}}))
    as_account(client, "manager")
    assert client.get("/api/v1/cad/jobs/cad_a0000000000000ad/input.png").status_code == 404
    code, out = run(capsys, "adopt-cad", "--owner", "manager")
    # 其他測試留下的舊資料夾（有摘要、沒有存取紀錄）也會一起補，所以只確認至少補了這一個
    assert code == 0 and "已補上" in out and (d / cad_access.ACCESS_FILE).is_file()
    record = json.loads((d / cad_access.ACCESS_FILE).read_text())
    assert record["owner_id"] == "manager" and record["image_id"] == "img_old0000000000001"
