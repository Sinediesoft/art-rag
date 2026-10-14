"""背景工作的測試環境（TASK-20261013-001）：隔離的 PostgreSQL、Redis、假 Timefold、Celery worker。

- PostgreSQL／Redis：在暫存資料夾 initdb／啟動，隨機埠，只綁 127.0.0.1；不連既有服務、不碰既有資料
- 假 Timefold：行程內的 HTTP 伺服器，回傳後端自己算出的合法排程與分數（可設定求解多久、可被停止）
- worker：獨立的 `celery -A app.jobs.celery_app worker` 子行程（不是 eager 模式），可殺掉、重啟
- 缺 PostgreSQL／Redis 時測試失敗（不是略過）並提示安裝；`ARTRAG_JOBS_INFRA_OPTIONAL=1` 才改略過
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest

BACKEND = Path(__file__).resolve().parents[1]
HINT = "需要 PostgreSQL 與 Redis 才能做背景工作的測試：brew install postgresql@17 redis"


def find_bin(name: str, extra: tuple[str, ...] = ()) -> str | None:
    import glob

    dirs = [
        *extra,
        "/opt/homebrew/opt/postgresql@17/bin",
        "/usr/local/opt/postgresql@17/bin",
        *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True),  # Debian／Ubuntu（apt）
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for d in dirs:
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _missing(what: str):
    if os.environ.get("ARTRAG_JOBS_INFRA_OPTIONAL") == "1":
        pytest.skip(f"沒有 {what}（ARTRAG_JOBS_INFRA_OPTIONAL=1）")
    pytest.fail(f"找不到 {what}。{HINT}", pytrace=False)


class PgServer:
    def __init__(self, root: Path):
        initdb, pg_ctl = find_bin("initdb"), find_bin("pg_ctl")
        if not initdb or not pg_ctl:
            _missing("PostgreSQL（initdb／pg_ctl）")
        self.pg_ctl = pg_ctl
        self.data = root / "pgdata"
        self.log = root / "postgres.log"
        self.port = free_port()
        subprocess.run(
            [
                initdb,
                "-D",
                str(self.data),
                "-U",
                "artrag",
                "-A",
                "trust",
                "--no-sync",
                "-E",
                "UTF8",
            ],
            check=True,
            capture_output=True,
        )
        with open(self.data / "postgresql.conf", "a", encoding="utf-8") as f:
            f.write(
                f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n"
                "unix_socket_directories = ''\nfsync = off\nsynchronous_commit = off\n"
                "full_page_writes = off\nmax_connections = 100\n"
            )
        self.start()
        with psycopg.connect(self.url("postgres"), autocommit=True) as c:
            c.execute("CREATE DATABASE artrag_jobs")

    def start(self) -> None:
        subprocess.run(
            [self.pg_ctl, "-D", str(self.data), "-l", str(self.log), "-w", "start"],
            check=True,
            capture_output=True,
        )

    def stop(self, mode: str = "fast") -> None:
        subprocess.run(
            [self.pg_ctl, "-D", str(self.data), "-m", mode, "-w", "stop"], capture_output=True
        )

    def url(self, db: str = "artrag_jobs") -> str:
        return f"postgresql://artrag@127.0.0.1:{self.port}/{db}"

    def terminate_backends(self) -> None:
        """砍掉所有連線（模擬資料庫短暫中斷）；伺服器本身還在。"""
        with psycopg.connect(self.url("postgres"), autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = 'artrag_jobs' AND pid <> pg_backend_pid()"
            )


class RedisServer:
    def __init__(self, root: Path):
        exe = find_bin("redis-server")
        if not exe:
            _missing("Redis（redis-server）")
        self.exe, self.root, self.port = exe, root, free_port()
        self.proc: subprocess.Popen | None = None
        self.start()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                self.exe,
                "--port",
                str(self.port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(self.root),
            ],  # fmt: skip
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import redis

        r = redis.Redis(port=self.port)
        for _ in range(100):
            try:
                if r.ping():
                    return
            except redis.RedisError:
                time.sleep(0.05)
        raise RuntimeError("redis-server 沒有啟動")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def flush(self) -> None:
        import redis

        redis.Redis(port=self.port).flushall()


# ---------------------------------------------------------------------------------- 假 Timefold
class FakeTimefold:
    """行程內的 HTTP 伺服器，模擬 Timefold 排程服務：回傳後端自己用 greedy_schedule 算出的
    合法排程與分數（所以後端重算的分數和它一致）。duration 秒後才算求解完成，期間 SOLVING；
    被要求 stop 就回 STOPPED（目前最佳解）。"""

    def __init__(self, duration: float = 3.0):
        self.duration = duration
        self.jobs: dict[str, dict] = {}
        self.submitted: list[str] = []
        self.stopped: list[str] = []
        self.corrupt_score = False  # True：回傳和後端重算不一致的分數
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: dict, code: int = 200):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/health"):
                    return self._send(
                        {
                            "version": "fake-1",
                            "java": "21",
                            "memory": {"committedMb": 10},
                            "activeJobs": len(owner.jobs),
                        }  # fmt: skip
                    )
                jid = self.path.split("?")[0].rsplit("/", 1)[-1]
                job = owner.jobs.get(jid)
                if job is None:
                    return self._send({"error": "no such job"}, 404)
                self._send(owner._status(job))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path == "/jobs":
                    return self._send({"jobId": owner._submit(json.loads(body or b"{}"))})
                if self.path.endswith("/stop"):
                    jid = self.path.split("/")[-2]
                    if jid in owner.jobs:
                        owner.jobs[jid]["stop"] = True
                        owner.stopped.append(jid)
                    return self._send({"ok": True})
                self._send({"error": "unknown"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _submit(self, problem: dict) -> str:
        from app.scheduling.problem import build_problem
        from app.scheduling.solution import evaluate, greedy_schedule

        p = build_problem()
        timed = greedy_schedule(p)
        ev = evaluate(p, timed)
        jid = uuid.uuid4().hex[:12]
        self.jobs[jid] = {"t0": time.time(), "timed": timed, "ev": ev, "stop": False}
        self.submitted.append(jid)
        return jid

    def _status(self, job: dict) -> dict:
        ev, timed = job["ev"], job["timed"]
        elapsed = time.time() - job["t0"]
        if job["stop"]:
            status = "STOPPED"
        elif elapsed >= self.duration:
            status = "SOLVED"
        else:
            status = "SOLVING"
        score = ev["score"] if not self.corrupt_score else "0hard/-1medium/-1soft"
        return {
            "status": status,
            "phase": "局部搜尋" if status == "SOLVING" else "完成",
            "elapsedMs": int(elapsed * 1000),
            "improvements": int(elapsed / 0.5) + 1,
            "initialScore": "0hard/-9999medium/-9999soft",
            "score": score,
            "hard": ev["hard"],
            "medium": ev["medium"],
            "soft": ev["soft"],
            "structural": 0,
            "feasible": ev["hard"] >= 0,
            "assignments": [
                {
                    "operationId": t["op_id"],
                    "machineId": t["machine_id"],
                    "startMin": t["start_min"],
                    "endMin": t["end_min"],
                    "setupMin": t.get("setup_min", 0),
                }
                for t in timed.values()
            ],
        }

    def close(self) -> None:
        self.server.shutdown()


# ---------------------------------------------------------------------------------- worker
class Worker:
    """獨立的 Celery worker 行程（pool=solo，一次處理一個工作）。"""

    def __init__(self, env: dict, queue: str, name: str, root: Path):
        self.name, self.queue, self.env = name, queue, env
        self.log_path = root / f"worker-{name}.log"
        self.proc: subprocess.Popen | None = None
        self.start()

    def start(self) -> None:
        self.log = open(self.log_path, "ab")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.jobs.celery_app",
                "worker",
                "-Q",
                self.queue,
                "--pool=solo",
                "-n",
                f"{self.name}@%h",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "-l",
                "INFO",
            ],  # fmt: skip
            cwd=BACKEND,
            env=self.env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if b" ready." in self.log_path.read_bytes():
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"worker {self.name} 啟動失敗：\n{self.tail()}")
            time.sleep(0.2)
        raise RuntimeError(f"worker {self.name} 逾時沒有 ready：\n{self.tail()}")

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])

    def kill(self) -> None:
        """SIGKILL：模擬 worker 當機（沒有機會收尾、租約也不會釋放）。"""
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)
        self.log.close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def restart(self) -> None:
        self.log_path.write_bytes(b"")
        self.start()


# ---------------------------------------------------------------------------------- 環境
JOB_ENV_KEYS = [
    "REDIS_URL", "JOBS_DATABASE_URL", "JOBS_LEASE_S", "JOBS_HEARTBEAT_S",
    "JOBS_DISPATCH_INTERVAL_S", "JOBS_POLL_S", "SCHEDULER_BASE_URL", "SCHEDULER_MODE",
    "JOBS_MAX_ATTEMPTS", "LEGACY_SYNC_JOBS", "JOBS_GPU_SLOTS", "JOBS_MODELS_RESTART_GRACE_S",
]  # fmt: skip


class JobsEnv:
    """測試用的工作系統環境：PostgreSQL、Redis、假 Timefold，以及該起的 worker。"""

    def __init__(self, root: Path, pg: PgServer, redis_srv: RedisServer, fake: FakeTimefold):
        self.root, self.pg, self.redis, self.fake = root, pg, redis_srv, fake
        self.workers: list[Worker] = []
        self.values = {
            "REDIS_URL": redis_srv.url,
            "JOBS_DATABASE_URL": pg.url(),
            "JOBS_LEASE_S": "4",
            "JOBS_HEARTBEAT_S": "0.4",
            "JOBS_DISPATCH_INTERVAL_S": "0.5",
            "JOBS_POLL_S": "0.1",
            "SCHEDULER_BASE_URL": fake.url,
            "SCHEDULER_MODE": "real",
            "LEGACY_SYNC_JOBS": "auto",
            # 測試的 Redis 是這個測試階段才啟動的：不等重啟寬限期（寬限期本身另有測試）
            "JOBS_MODELS_RESTART_GRACE_S": "0",
        }

    def worker_env(self) -> dict:
        env = {**os.environ, **self.values}
        env.update(
            {"EMBED_MODE": "mock", "LLM_MODE": "mock", "MEMORY_GUARD": "false", "DATABASE_URL": ""}
        )
        return env

    def start_worker(self, queue: str, name: str) -> Worker:
        w = Worker(self.worker_env(), queue, name, self.root)
        self.workers.append(w)
        return w

    def stop_workers(self) -> None:
        for w in self.workers:
            w.stop()
        self.workers.clear()

    def reset(self) -> None:
        """每個測試前：停掉 worker、清空工作表與 Redis、刪掉產物資料夾。"""
        self.stop_workers()
        self.redis.flush()
        with psycopg.connect(self.pg.url(), autocommit=True) as c:
            c.execute(
                "TRUNCATE jobs, job_events, job_outbox, job_artifacts, resource_leases CASCADE"
            )
        self.fake.jobs.clear()
        self.fake.submitted.clear()
        self.fake.stopped.clear()
        self.fake.corrupt_score = False
        self.fake.duration = 3.0

    def sql(self, query: str, params: tuple = ()) -> list[dict]:
        from psycopg.rows import dict_row

        with psycopg.connect(self.pg.url(), autocommit=True, row_factory=dict_row) as c:
            cur = c.execute(query, params)
            return cur.fetchall() if cur.description else []

    def job(self, job_id: str) -> dict:
        rows = self.sql("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return rows[0]

    def wait_status(self, job_id: str, statuses, timeout: float = 90.0) -> dict:
        statuses = (statuses,) if isinstance(statuses, str) else tuple(statuses)
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.job(job_id)
            if last["status"] in statuses:
                return last
            time.sleep(0.15)
        logs = "\n".join(f"--- {w.name} ---\n{w.tail(25)}" for w in self.workers)
        raise AssertionError(
            f"工作 {job_id} 等不到 {statuses}，目前 {last['status'] if last else None}"
            f"（attempt={last['attempt'] if last else None}）\n{logs}"
        )

    def wait_until(self, fn, timeout: float = 60.0, what: str = ""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = fn()
            if v:
                return v
            time.sleep(0.15)
        raise AssertionError(f"逾時：{what}")


@pytest.fixture(scope="session")
def infra_servers(tmp_path_factory):
    root = Path(tempfile.mkdtemp(prefix="artrag-jobs-"))
    pg = redis_srv = fake = None
    try:
        pg = PgServer(root)
        redis_srv = RedisServer(root)
        fake = FakeTimefold()
        yield root, pg, redis_srv, fake
    finally:
        if fake:
            fake.close()
        if redis_srv:
            redis_srv.stop()
        if pg:
            pg.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def jobs_env(infra_servers, mock_env):
    """工作系統啟用的環境（這個測試模組內）：設定指向隔離的 PostgreSQL／Redis／假 Timefold，
    已套用 migration；結束時還原設定。"""
    from app.core import config
    from app.jobs import broker, db, migrate
    from app.services import cad_access

    root, pg, redis_srv, fake = infra_servers
    env = JobsEnv(root, pg, redis_srv, fake)
    saved = {k: os.environ.get(k) for k in JOB_ENV_KEYS}
    os.environ.update(env.values)
    config.get_settings.cache_clear()
    db.close()
    broker._url["broker"] = None
    migrate.apply()
    from app.jobs import boot

    cad_access.set_job_lookup(boot._lookup)
    try:
        yield env
    finally:
        env.stop_workers()
        cad_access.set_job_lookup(None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.get_settings.cache_clear()
        db.close()
        broker._url["broker"] = None


@pytest.fixture
def jobs(jobs_env):
    jobs_env.reset()
    yield jobs_env
    jobs_env.stop_workers()


class LiveApi:
    """真的在 socket 上跑的 API（uvicorn 執行緒）：TestClient 不會邊收邊給，測串流中途要用它。"""

    def __init__(self):
        import uvicorn

        from app.main import app

        self.port = free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 120
        while not self.server.started and time.time() < deadline:
            time.sleep(0.1)
        if not self.server.started:
            raise RuntimeError("uvicorn 沒有啟動")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def headers(self, account_id: str) -> dict:
        from app.services import identity

        return {"Authorization": "Bearer " + identity.issue(identity.get_account(account_id))}

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


@pytest.fixture(scope="module")
def live_api(jobs_env):
    api = LiveApi()
    try:
        yield api
    finally:
        api.close()
