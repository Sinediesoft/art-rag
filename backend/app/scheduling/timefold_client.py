"""Timefold 排程服務（scheduler/，make scheduler）的用戶端。只准連本機或內網位址。"""

import httpx

from app.core.config import get_settings
from app.rag.providers import is_local_url


class SchedulerUnavailable(Exception):
    pass


def _base() -> str:
    url = get_settings().scheduler_base_url.rstrip("/")
    if not is_local_url(url):
        raise SchedulerUnavailable(f"{url} 不是本機或內網位址，拒絕連線")
    return url


async def _call(method: str, path: str, **kw) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0)) as c:
            r = await c.request(method, _base() + path, **kw)
    except httpx.HTTPError as e:
        raise SchedulerUnavailable(
            f"連不上排程服務 {get_settings().scheduler_base_url}：{type(e).__name__}"
        ) from e
    if r.status_code >= 400:
        detail = (
            r.json().get("error", r.text)
            if r.headers.get("content-type", "").startswith("application/json")
            else r.text
        )
        raise SchedulerUnavailable(f"排程服務回應 HTTP {r.status_code}：{detail}")
    return r.json()


async def health() -> dict | None:
    """排程服務的版本與 JVM 記憶體；連不上回 None。"""
    try:
        return await _call("GET", "/health")
    except SchedulerUnavailable:
        return None


async def submit(problem: dict) -> str:
    return (await _call("POST", "/jobs", json=problem))["jobId"]


async def status(job_id: str, solution: bool = True) -> dict:
    return await _call("GET", f"/jobs/{job_id}", params={"solution": 1} if solution else None)


async def stop(job_id: str) -> None:
    await _call("POST", f"/jobs/{job_id}/stop")
