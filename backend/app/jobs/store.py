"""工作資料庫的存取層：SQL 只寫在這裡（TASK-20261013-001，docs/adr/036）。

狀態轉移（`queued → running → succeeded／failed／cancelled`），每一步都是帶條件的單一 UPDATE：

| 動作 | 條件（不符就是 0 列，呼叫端依回傳處理） |
|---|---|
| claim | status = queued，沒被要求取消；GPU 名額／排程範圍租約拿得到 |
| heartbeat／事件／進度 | status = running，attempt 與 lease_owner 都是自己 |
| reserve_commit | 同上，而且沒被要求取消——之後取消回 409，成功寫回 |
| finish | 同上；succeeded 還要 committing_attempt = attempt |
| cancel | queued → 直接 cancelled；running 且沒進入寫回 → cancel_requested |
| recover_expired | running 但租約過期，而且沒有 worker 正在寫回（advisory lock）→ 回佇列或失敗 |

所有時間都用資料庫的 now()，不依賴 worker 的時鐘。
"""

import hashlib
import json
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import get_settings
from app.core.errors import AppError
from app.jobs import db

QUEUE_OF = {"cad": "gpu", "schedule": "schedule"}
ID_PREFIX = {"cad": "cad_", "schedule": "sch_"}
TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")


class LeaseLost(Exception):
    """這個 worker 的執行嘗試已不是現行的（租約過期被接手、工作被取消或結束）：不能再寫任何東西。"""


class ScheduleBusy(AppError):
    def __init__(self, active_job_id: str):
        super().__init__(
            "SCHEDULE_BUSY",
            f"已有排程工作 {active_job_id} 在排隊或執行，請等它完成、提前結束或取消",
            409,
        )
        self.active_job_id = active_job_id


def new_job_id(kind: str) -> str:
    return ID_PREFIX[kind] + secrets.token_hex(8)


def request_hash(kind: str, params: dict) -> str:
    body = json.dumps({"kind": kind, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def _lock_key(job_id: str) -> str:
    return job_id


def _event(conn, job_id: str, type_: str, data: dict | None = None) -> int:
    row = conn.execute(
        "UPDATE jobs SET event_seq = event_seq + 1, updated_at = now() WHERE id = %s"
        " RETURNING event_seq",
        (job_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO job_events (job_id, seq, type, data) VALUES (%s, %s, %s, %s)",
        (job_id, row["event_seq"], type_, Jsonb(data or {})),
    )
    return row["event_seq"]


def iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def get(job_id: str) -> dict | None:
    with db.connection() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def get_for_update(conn, job_id: str) -> dict | None:
    return conn.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)).fetchone()


def list_jobs(
    *,
    kind: str | None = None,
    owner_id: str | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = 20,
) -> list[dict]:
    where, params = ["TRUE"], []
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if owner_id:
        where.append("owner_id = %s")
        params.append(owner_id)
    if statuses:
        where.append("status = ANY(%s)")
        params.append(list(statuses))
    with db.connection() as conn:
        return conn.execute(
            f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s",
            (*params, limit),
        ).fetchall()


def artifacts(job_id: str) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT name, content_type, size_bytes, sha256 FROM job_artifacts WHERE job_id = %s"
            " ORDER BY name",
            (job_id,),
        ).fetchall()


# ----------------------------------------------------------------------------- 建立（含 outbox）
def create_job(
    *,
    kind: str,
    owner_id: str,
    params: dict,
    resource: dict,
    scope: str,
    request_id: str,
    input_version: str | None = None,
    input_versions: dict | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, bool]:
    """建立工作：jobs 列、created 事件與 outbox 在同一個交易。回傳 (工作, 是否新建)。

    相同帳號／操作／key 的相同請求取得同一個工作（False）；參數不同回 409 IDEMPOTENCY_KEY_REUSED。
    排程範圍已有排隊或執行中的工作回 409 SCHEDULE_BUSY。"""
    s = get_settings()
    rhash = request_hash(kind, {**params, "resource": resource})
    job_id = new_job_id(kind)
    # 外層 transaction()：jobs 列、created 事件、outbox 一起提交或一起回滾
    # （沒有先執行過任何語句時 transaction() 是最外層交易，不能只包住 INSERT）
    with db.connection() as conn, conn.transaction():
        if idempotency_key:
            existing = _by_key(conn, owner_id, kind, idempotency_key)
            if existing:
                return _replay(existing, rhash), False
        if kind == "cad":
            active = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE owner_id = %s AND kind = 'cad'"
                " AND status IN ('queued', 'running')",
                (owner_id,),
            ).fetchone()["n"]
            if active >= s.jobs_max_active_per_owner:
                raise AppError(
                    "JOBS_LIMIT_REACHED",
                    f"您已有 {active} 個 3D 重建在排隊或執行中"
                    f"（上限 {s.jobs_max_active_per_owner}），請等它們完成或取消幾個再建立",
                    429,
                )
        try:
            with conn.transaction():  # savepoint：唯一約束衝突時只回滾這個 INSERT
                conn.execute(
                    "INSERT INTO jobs (id, kind, queue, owner_id, scope, params, resource,"
                    " input_version, input_versions, idempotency_key, request_hash, request_id,"
                    " max_attempts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        job_id,
                        kind,
                        QUEUE_OF[kind],
                        owner_id,
                        scope,
                        Jsonb(params),
                        Jsonb(resource),
                        input_version,
                        Jsonb(input_versions) if input_versions is not None else None,
                        idempotency_key,
                        rhash,
                        request_id,
                        s.jobs_max_attempts,
                    ),
                )
        except psycopg.errors.UniqueViolation as e:
            name = e.diag.constraint_name or ""
            if "idempotency" in name and idempotency_key:  # 同時送兩次：另一個交易先寫入
                existing = _by_key(conn, owner_id, kind, idempotency_key)
                if existing:
                    return _replay(existing, rhash), False
            active = conn.execute(
                "SELECT id FROM jobs WHERE scope = %s AND status IN ('queued', 'running')"
                " ORDER BY created_at LIMIT 1",
                (scope,),
            ).fetchone()
            if active:
                raise ScheduleBusy(active["id"]) from e
            raise
        _event(conn, job_id, "created", {"status": "queued", "kind": kind})
        conn.execute(
            "INSERT INTO job_outbox (job_id, queue, reason) VALUES (%s, %s, 'created')",
            (job_id, QUEUE_OF[kind]),
        )
        return conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone(), True


def _by_key(conn, owner_id: str, kind: str, key: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE owner_id = %s AND kind = %s AND idempotency_key = %s",
        (owner_id, kind, key),
    ).fetchone()


def _replay(existing: dict, rhash: str) -> dict:
    if existing["request_hash"] != rhash:
        raise AppError(
            "IDEMPOTENCY_KEY_REUSED",
            "這個 Idempotency-Key 已經用在參數不同的請求上，請換一個 key 或使用相同的參數",
            409,
        )
    return existing


# ----------------------------------------------------------------------------- 領取與租約
@dataclass
class Claim:
    state: str  # claimed／blocked／gone／not_queued／cancelled
    job: dict | None = None
    resource: str | None = None


def _acquire_resource(conn, job: dict, lease_s: float) -> str | None:
    """排程：範圍租約；3D 重建：第一個空的 GPU 名額。拿不到回 None。過期的租約直接接手。"""
    if job["kind"] == "schedule":
        candidates = [job["scope"]]
    else:
        candidates = [f"gpu:{i}" for i in range(max(1, get_settings().jobs_gpu_slots))]
    for res in candidates:
        got = conn.execute(
            "INSERT INTO resource_leases (resource, holder, attempt, expires_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))"
            " ON CONFLICT (resource) DO UPDATE SET holder = EXCLUDED.holder,"
            " attempt = EXCLUDED.attempt, fencing = resource_leases.fencing + 1,"
            " expires_at = EXCLUDED.expires_at, updated_at = now()"
            " WHERE resource_leases.expires_at < now() OR resource_leases.holder = EXCLUDED.holder"
            " RETURNING resource",
            (res, job["id"], job["attempt"] + 1, lease_s),
        ).fetchone()
        if got:
            return res
    return None


def claim(job_id: str, worker_id: str) -> Claim:
    s = get_settings()
    with db.connection() as conn:
        job = get_for_update(conn, job_id)
        if job is None:
            return Claim("gone")
        if job["status"] != "queued":
            return Claim("not_queued", job)
        if job["cancel_requested"]:
            _finish_locked(conn, job, "cancelled", None, None, "已在排隊時取消")
            return Claim("cancelled", job)
        res = _acquire_resource(conn, job, s.jobs_lease_s)
        if res is None:
            return Claim("blocked", job)
        row = conn.execute(
            "UPDATE jobs SET status = 'running', attempt = attempt + 1, lease_owner = %s,"
            " lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),"
            " started_at = COALESCE(started_at, now()), committing_attempt = NULL,"
            " stop_requested = FALSE WHERE id = %s RETURNING *",
            (worker_id, s.jobs_lease_s, job_id),
        ).fetchone()
        _event(conn, job_id, "started", {"attempt": row["attempt"], "resource": res})
        return Claim("claimed", row, res)


# 工作目前持有自己的資源租約（GPU 名額或排程範圍）：holder 是工作 id、attempt 是這次嘗試。
# 租約過期後被別的工作接手，holder 就變了——舊 worker 在 recover_expired 把它收回之前醒來，
# 靠這個條件認出自己已經失去資源（AC-14：GPU 重工作同時最多一個）。
_HOLDS_RESOURCE = (
    "EXISTS (SELECT 1 FROM resource_leases r WHERE r.holder = jobs.id AND r.attempt = jobs.attempt)"
)


def heartbeat(job_id: str, attempt: int, worker_id: str) -> dict:
    """續約並回報旗標。工作租約與資源租約都還是自己的才成功；否則丟 LeaseLost，
    連同工作列的更新一起回滾（不替已經失去資源的 worker 延長租約）。"""
    lease = get_settings().jobs_lease_s
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s),"
            " heartbeat_at = now() WHERE id = %s AND status = 'running' AND attempt = %s"
            " AND lease_owner = %s RETURNING cancel_requested, stop_requested, committing_attempt",
            (lease, job_id, attempt, worker_id),
        ).fetchone()
        if row is None:
            raise LeaseLost(job_id)
        # 寫回區段中的資源租約是 'infinity'（見 reserve_commit），續約不能把它改回有期限的
        held = conn.execute(
            "UPDATE resource_leases SET expires_at = CASE WHEN expires_at = 'infinity'"
            " THEN expires_at ELSE now() + make_interval(secs => %s) END, updated_at = now()"
            " WHERE holder = %s AND attempt = %s RETURNING resource",
            (lease, job_id, attempt),
        ).fetchone()
        if held is None:  # 資源已被別的工作接手（或租約被收回）
            raise LeaseLost(job_id)
        return row


def _fenced(conn, job_id: str, attempt: int, worker_id: str) -> dict:
    row = conn.execute(
        "SELECT id FROM jobs WHERE id = %s AND status = 'running' AND attempt = %s"
        f" AND lease_owner = %s AND {_HOLDS_RESOURCE} FOR UPDATE",
        (job_id, attempt, worker_id),
    ).fetchone()
    if row is None:
        raise LeaseLost(job_id)
    return row


def add_event(
    job_id: str,
    attempt: int,
    worker_id: str,
    type_: str,
    data: dict | None = None,
    progress: dict | None = None,
) -> int:
    """執行中的事件（stage／progress…）。progress 有值就同時更新最新進度快照。"""
    with db.connection() as conn:
        _fenced(conn, job_id, attempt, worker_id)
        if progress is not None:
            conn.execute("UPDATE jobs SET progress = %s WHERE id = %s", (Jsonb(progress), job_id))
        return _event(conn, job_id, type_, data)


def reserve_commit(job_id: str, attempt: int, worker_id: str) -> bool:
    """進入寫回：沒被要求取消、而且仍是現行嘗試並持有資源租約才成功。
    成功後取消請求回 409（已經不可取消），一定寫回。回 False＝被要求取消，或已不是現行嘗試／
    資源已被接手（呼叫端再用 heartbeat 分辨：租約遺失會丟 LeaseLost）。

    成功的同時把這個工作的資源租約標成 'infinity'（永不過期）：從取得寫回資格到工作結束（或
    被恢復迴圈收回）為止，沒有別的工作（GPU 名額）或舊同步入口（排程範圍）能因為租約過期而
    接手——互斥涵蓋「整段」外部寫回，不是只在寫回前檢查一次。worker 若在這段期間死掉，工作租約
    會過期、寫回鎖隨連線釋放，恢復迴圈收回工作時一併刪掉資源租約。"""
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE jobs SET committing_attempt = attempt WHERE id = %s AND status = 'running'"
            " AND attempt = %s AND lease_owner = %s AND NOT cancel_requested"
            f" AND {_HOLDS_RESOURCE} RETURNING id",
            (job_id, attempt, worker_id),
        ).fetchone()
        if row is None:
            return False
        pinned = conn.execute(
            "UPDATE resource_leases SET expires_at = 'infinity', updated_at = now()"
            " WHERE holder = %s AND attempt = %s RETURNING resource",
            (job_id, attempt),
        ).fetchone()
        if pinned is None:  # 兩個語句之間資源被別的工作接手了：整個放棄（工作列的更新也回滾）
            conn.rollback()
            return False
        _event(conn, job_id, "committing", {"attempt": attempt})
        return True


class CommitSection:
    """寫回區段：持有寫回鎖（advisory lock，連線斷了自動釋放）的那條連線。

    fence() 要在每一個不可回滾的外部寫入（發布 CAD 產物、寫入排程結果）**之前**呼叫，
    而且是在這條持有寫回鎖的連線上查：連線還活著，寫回鎖就還在，恢復迴圈不會把工作收回；
    資源租約仍是 'infinity' 且 holder 還是自己，就沒有人接手過。連線斷了或被接手，丟例外、
    不寫入。"""

    def __init__(self, conn, job_id: str, attempt: int, worker_id: str):
        self._conn, self.job_id, self.attempt, self.worker_id = conn, job_id, attempt, worker_id

    def fence(self) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM jobs j WHERE j.id = %s AND j.status = 'running' AND j.attempt = %s"
            " AND j.lease_owner = %s AND j.committing_attempt = j.attempt AND EXISTS ("
            "SELECT 1 FROM resource_leases r WHERE r.holder = j.id AND r.attempt = j.attempt"
            " AND r.expires_at = 'infinity')",
            (self.job_id, self.attempt, self.worker_id),
        ).fetchone()
        if row is None:
            raise LeaseLost(self.job_id)


@contextmanager
def commit_section(job_id: str, attempt: int, worker_id: str) -> Iterator[CommitSection]:
    """取得寫回鎖並確認（fence）仍是現行嘗試、已取得寫回資格、資源租約沒被接手。
    寫回鎖讓恢復迴圈遇到就跳過，租約剛好過期的舊 worker 不會和接手的新 worker 同時寫回。"""
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (_lock_key(job_id),))
        try:
            section = CommitSection(conn, job_id, attempt, worker_id)
            section.fence()
            yield section
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_lock_key(job_id),))


@contextmanager
def commit_lock(job_id: str) -> Iterator[None]:
    """只有寫回鎖、沒有 fencing 的版本（測試與診斷用）；工作寫回一律用 commit_section。"""
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (_lock_key(job_id),))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_lock_key(job_id),))


def finish(
    job_id: str,
    attempt: int,
    worker_id: str,
    status: str,
    *,
    result: dict | None = None,
    error: dict | None = None,
    note: str | None = None,
    artifact_rows: list[dict] | None = None,
) -> None:
    """結束工作（fenced）。租約已不是自己的丟 LeaseLost，什麼都不寫。"""
    assert status in TERMINAL
    with db.connection() as conn:
        _fenced(conn, job_id, attempt, worker_id)
        job = get_for_update(conn, job_id)
        if status == "succeeded" and job["committing_attempt"] != attempt:
            raise LeaseLost(job_id)  # 沒有 reserve_commit 不能成功
        for a in artifact_rows or []:
            conn.execute(
                "INSERT INTO job_artifacts (job_id, name, content_type, size_bytes, sha256)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (job_id, name) DO UPDATE SET"
                " content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes,"
                " sha256 = EXCLUDED.sha256, created_at = now()",
                (job_id, a["name"], a["content_type"], a["size_bytes"], a["sha256"]),
            )
        _finish_locked(conn, job, status, result, error, note)


def _finish_locked(conn, job: dict, status: str, result, error, note) -> None:
    conn.execute(
        "UPDATE jobs SET status = %s, result = %s, error = %s, finished_at = now(),"
        " lease_owner = NULL, lease_expires_at = NULL, stop_requested = FALSE WHERE id = %s",
        (
            status,
            Jsonb(result) if result is not None else None,
            Jsonb(error) if error is not None else None,
            job["id"],
        ),
    )
    conn.execute("DELETE FROM resource_leases WHERE holder = %s", (job["id"],))
    data: dict[str, Any] = {"status": status}
    if error:
        data.update(error)
    if note:
        data["note"] = note
    if result is not None and status == "succeeded":
        data["result"] = result
    _event(conn, job["id"], status, data)


# ----------------------------------------------------------------------------- 取消與提前停止
def cancel(job_id: str) -> tuple[str, dict]:
    """回傳 (結果, 工作)。結果：cancelled（排隊中直接取消）、cancelling（執行中，worker 會停止）、
    already_cancelled、too_late（已進入寫回，會成功）、finished（已經成功或失敗）。"""
    with db.connection() as conn:
        job = get_for_update(conn, job_id)
        if job is None:
            raise AppError("JOB_NOT_FOUND", "找不到這個工作，可能已超過保存期限", 404)
        if job["status"] == "cancelled":
            return "already_cancelled", job
        if job["status"] in ("succeeded", "failed"):
            return "finished", job
        if job["status"] == "queued":
            _finish_locked(conn, job, "cancelled", None, None, "排隊中取消，沒有執行")
            return "cancelled", conn.execute(
                "SELECT * FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        if job["committing_attempt"] is not None and job["committing_attempt"] == job["attempt"]:
            return "too_late", job
        if not job["cancel_requested"]:
            conn.execute("UPDATE jobs SET cancel_requested = TRUE WHERE id = %s", (job_id,))
            _event(conn, job_id, "cancel_requested", {"attempt": job["attempt"]})
        return "cancelling", conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def request_stop(job_id: str) -> tuple[str, dict]:
    """排程提前停止（採用目前最佳的合法解）：只有執行中、還沒進入寫回、沒被要求取消的工作。"""
    with db.connection() as conn:
        job = get_for_update(conn, job_id)
        if job is None:
            raise AppError("JOB_NOT_FOUND", "找不到這個工作，可能已超過保存期限", 404)
        if job["status"] != "running":
            return "not_running", job
        if job["cancel_requested"]:
            return "cancelling", job
        if job["committing_attempt"] is not None and job["committing_attempt"] == job["attempt"]:
            return "too_late", job
        if not job["stop_requested"]:
            conn.execute("UPDATE jobs SET stop_requested = TRUE WHERE id = %s", (job_id,))
            _event(conn, job_id, "stop_requested", {"attempt": job["attempt"]})
        return "stopping", conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()


def active_job(scope: str) -> dict | None:
    with db.connection() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE scope = %s AND status IN ('queued', 'running')"
            " ORDER BY created_at LIMIT 1",
            (scope,),
        ).fetchone()


# ----------------------------------------------------------------------------- 事件
def events_after(job_id: str, after_seq: int, limit: int = 200) -> list[dict]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT seq, type, data, at FROM job_events WHERE job_id = %s AND seq > %s"
            " ORDER BY seq LIMIT %s",
            (job_id, after_seq, limit),
        ).fetchall()


# ----------------------------------------------------------------------------- 恢復與投遞
def recover_expired(limit: int = 20) -> list[tuple[str, str]]:
    """租約過期的執行中工作：要求取消的 → cancelled；用完嘗試次數 → failed；其餘回佇列重投。
    有 worker 正在寫回（持有 advisory lock）的跳過。回傳 [(job_id, 動作)]。"""
    out = []
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'running' AND lease_expires_at < now()"
            " ORDER BY lease_expires_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        ).fetchall()
        for r in rows:
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS ok", (r["id"],)
            ).fetchone()["ok"]
            if not locked:
                continue
            job = get_for_update(conn, r["id"])
            if job["status"] != "running" or job["lease_expires_at"] is None:
                continue
            if job["cancel_requested"]:
                _finish_locked(conn, job, "cancelled", None, None, "worker 中斷，依取消要求結束")
                out.append((job["id"], "cancelled"))
            elif job["attempt"] >= job["max_attempts"]:
                err = {
                    "code": "LEASE_EXPIRED",
                    "message": f"worker 連續 {job['attempt']} 次中斷，已停止重試",
                }
                _finish_locked(conn, job, "failed", None, err, None)
                out.append((job["id"], "failed"))
            else:
                conn.execute(
                    "UPDATE jobs SET status = 'queued', lease_owner = NULL,"
                    " lease_expires_at = NULL, committing_attempt = NULL, stop_requested = FALSE"
                    " WHERE id = %s",
                    (job["id"],),
                )
                conn.execute("DELETE FROM resource_leases WHERE holder = %s", (job["id"],))
                conn.execute(
                    "INSERT INTO job_outbox (job_id, queue, reason) VALUES (%s, %s, 'recovered')",
                    (job["id"], job["queue"]),
                )
                _event(
                    conn,
                    job["id"],
                    "requeued",
                    {"reason": "worker 租約過期，重新排入佇列", "attempt": job["attempt"]},
                )
                out.append((job["id"], "requeued"))
    return out


def dispatch_pending(publish: Callable[[str, str], None], limit: int = 20) -> tuple[int, int]:
    """把 outbox 裡還沒投遞的訊息送進 broker。publish 失敗只記錄並延後重試（指數退讓），
    不影響其他訊息。至少一次投遞：重複的訊息由 claim 擋掉。回傳 (成功數, 失敗數)。"""
    sent = failed = 0
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, job_id, queue, dispatch_attempts FROM job_outbox"
            " WHERE dispatched_at IS NULL AND available_at <= now() ORDER BY id LIMIT %s"
            " FOR UPDATE SKIP LOCKED",
            (limit,),
        ).fetchall()
        for r in rows:
            try:
                publish(r["job_id"], r["queue"])
            except Exception as e:  # noqa: BLE001 — broker 的各種連線錯誤都延後重試
                backoff = min(10, 2 ** min(r["dispatch_attempts"], 5))  # 最久等 10 秒就再試
                conn.execute(
                    "UPDATE job_outbox SET dispatch_attempts = dispatch_attempts + 1,"
                    " available_at = now() + make_interval(secs => %s), last_error = %s"
                    " WHERE id = %s",
                    (backoff, f"{type(e).__name__}: {e}"[:300], r["id"]),
                )
                failed += 1
                continue
            conn.execute("UPDATE job_outbox SET dispatched_at = now() WHERE id = %s", (r["id"],))
            conn.execute("UPDATE jobs SET dispatched_at = now() WHERE id = %s", (r["job_id"],))
            sent += 1
    return sent, failed


def redispatch_stalled(stall_s: float) -> int:
    """排隊中卻沒有未投遞訊息、而且很久沒投遞過的工作（broker 重啟、訊息遺失）：再投一次。"""
    with db.connection() as conn:
        rows = conn.execute(
            "INSERT INTO job_outbox (job_id, queue, reason)"
            " SELECT j.id, j.queue, 'stalled' FROM jobs j WHERE j.status = 'queued'"
            " AND COALESCE(j.dispatched_at, j.created_at) < now() - make_interval(secs => %s)"
            " AND NOT EXISTS (SELECT 1 FROM job_outbox o WHERE o.job_id = j.id"
            " AND o.dispatched_at IS NULL) RETURNING job_id",
            (stall_s,),
        ).fetchall()
    return len(rows)


def pending_outbox() -> int:
    with db.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM job_outbox WHERE dispatched_at IS NULL"
        ).fetchone()["n"]


def purge(older_than_days: float) -> list[tuple[str, str]]:
    """刪掉結束超過保存期限的工作（事件、outbox、產物紀錄一起）。回傳 [(id, kind)]。"""
    with db.connection() as conn:
        rows = conn.execute(
            "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled')"
            " AND finished_at < now() - make_interval(days => %s) RETURNING id, kind",
            (older_than_days,),
        ).fetchall()
    return [(r["id"], r["kind"]) for r in rows]


def counts() -> dict[str, int]:
    with db.connection() as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ----------------------------------------------------------------------------- 資源租約（舊入口用）
def acquire_lease(resource: str, holder: str, ttl_s: float, attempt: int = 0) -> bool:
    """拿資源租約（過期的直接接手，同一個 holder 可重入）。排程範圍同時只有一個持有者。"""
    with db.connection() as conn:
        row = conn.execute(
            "INSERT INTO resource_leases (resource, holder, attempt, expires_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))"
            " ON CONFLICT (resource) DO UPDATE SET holder = EXCLUDED.holder,"
            " attempt = EXCLUDED.attempt, fencing = resource_leases.fencing + 1,"
            " expires_at = EXCLUDED.expires_at, updated_at = now()"
            " WHERE resource_leases.expires_at < now() OR resource_leases.holder = EXCLUDED.holder"
            " RETURNING resource",
            (resource, holder, attempt, ttl_s),
        ).fetchone()
        return row is not None


def renew_lease(resource: str, holder: str, ttl_s: float) -> bool:
    with db.connection() as conn:
        row = conn.execute(
            "UPDATE resource_leases SET expires_at = now() + make_interval(secs => %s),"
            " updated_at = now() WHERE resource = %s AND holder = %s RETURNING resource",
            (ttl_s, resource, holder),
        ).fetchone()
        return row is not None


def release_lease(resource: str, holder: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "DELETE FROM resource_leases WHERE resource = %s AND holder = %s", (resource, holder)
        )


def update_resource(job_id: str, attempt: int, worker_id: str, changes: dict) -> None:
    """執行中得知的來源資源（例如照片辨識出的知識庫圖紙）補進 resource，讀取授權會一併檢查。"""
    with db.connection() as conn:
        _fenced(conn, job_id, attempt, worker_id)
        conn.execute(
            "UPDATE jobs SET resource = resource || %s WHERE id = %s", (Jsonb(changes), job_id)
        )
