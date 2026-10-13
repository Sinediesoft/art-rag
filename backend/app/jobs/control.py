"""執行中的工作控制：租約心跳、取消／提前停止旗標。

心跳執行緒每隔 JOBS_HEARTBEAT_S 續約並讀回資料庫裡的 cancel_requested／stop_requested；
租約已經不是自己的（過期被接手）就標成 lost——之後 check() 一律丟 LeaseLost，
worker 不能再寫任何事件、產物或結果。資料庫暫時連不上時繼續嘗試，但超過租約時間還沒成功
續約就當作租約已失效（因為別的 worker 可能已經接手）。
"""

import logging
import threading
import time

from app.core.config import get_settings
from app.jobs import db, store
from app.services import memory_guard

_log = logging.getLogger("artrag.jobs")


class JobCancelled(Exception):
    """工作被取消：停止執行，不寫回任何結果。"""


class JobControl:
    def __init__(self, job_id: str, attempt: int, worker_id: str):
        self.job_id, self.attempt, self.worker_id = job_id, attempt, worker_id
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._done = threading.Event()
        self._last_ok = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"hb-{job_id}")

    def start(self) -> "JobControl":
        self._thread.start()
        return self

    def shutdown(self) -> None:
        self._done.set()
        self._thread.join(timeout=2)

    # ---- 旗標
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def should_stop(self) -> bool:
        """提前停止（採用目前最佳的合法解）。"""
        return self._stop.is_set()

    def should_abort(self) -> bool:
        return self._cancel.is_set() or self._lost.is_set()

    def check(self, stage: str = "") -> None:
        if self._lost.is_set():
            raise store.LeaseLost(self.job_id)
        if self._cancel.is_set():
            raise JobCancelled(stage)

    # ---- 心跳
    def _loop(self) -> None:
        s = get_settings()
        while not self._done.wait(s.jobs_heartbeat_s):
            try:
                flags = store.heartbeat(self.job_id, self.attempt, self.worker_id)
                self._last_ok = time.monotonic()
                if flags["cancel_requested"]:
                    self._cancel.set()
                if flags["stop_requested"]:
                    self._stop.set()
                memory_guard.guard.publish()  # 續約「使用中的模型」登記
            except store.LeaseLost:
                self._lost.set()
                return
            except db.JobsUnavailable:
                if time.monotonic() - self._last_ok > s.jobs_lease_s:
                    self._lost.set()  # 超過租約時間都沒能續約：別的 worker 可能已經接手
                    return
            except Exception as e:  # noqa: BLE001
                _log.warning("job heartbeat failed: %s", e)
