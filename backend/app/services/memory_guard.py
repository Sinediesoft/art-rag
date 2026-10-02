"""記憶體管理：系統記憶體使用率超過門檻（預設 80%）時，釋放「目前流程用不到」的模型。

16 GB 的 Mac 要同時放 Qwen3-VL（Ollama）、Ortho2CAD（llama-server）、Chinese-CLIP 與 bge-m3
（後端行程內）和 Timefold 排程服務，全部載入會超過 80%。每個流程只用其中幾個：

| 流程 | 用到的模型 |
|---|---|
| 以圖搜圖／圖紙辨識 | Chinese-CLIP |
| 以文搜尋 | Chinese-CLIP、bge-m3 |
| 圖文問答／製程問答 | bge-m3、Qwen3-VL（照片辨識另加 Chinese-CLIP） |
| 3D 重建 | Ortho2CAD（照片另加 Chinese-CLIP；未收錄圖紙讀尺寸另加 Qwen3-VL） |
| 庫存查詢（Text-to-SQL） | Qwen3-VL |
| 生產排程 | Timefold（不用任何 AI 模型） |
| 照片建檔 | Chinese-CLIP、Qwen3-VL（讀標題欄）；收錄後重建索引另用 bge-m3 |

觸發時機：進入流程時檢查一次——把這個流程還要載入的模型算進去，預估超過門檻就先釋放；
背景每 5 秒再檢查一次（保留最近一次流程的模型）。
其他請求正在使用的模型（引用計數 > 0）一律不釋放，不會打斷生成中的回答。
釋放方式：Ollama 送 keep_alive=0、llama-server（router 模式）呼叫 /models/unload、
後端行程內的 embedding 模型直接卸載、Timefold 排程服務做 GC。下次用到時都會自動重新載入。
推論伺服器不在本機（例如組員連 5070 Ti）時不動它：釋放遠端記憶體對本機沒有幫助。
"""

import asyncio
import functools
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import psutil

from app.core.config import get_models_config, get_settings
from app.core.logging import log

FLOWS: dict[str, str] = {
    "search_image": "以圖搜圖／圖紙辨識",
    "search_text": "以文搜尋",
    "chat": "圖文問答",
    "reconstruct": "3D 重建",
    "sql": "庫存查詢",
    "schedule": "生產排程",
    "route": "智慧助理路由",
    "intake": "照片建檔",
    "intake_index": "照片建檔：重建索引",
    "manual": "手動釋放",
}


def memory_percent() -> float:
    return float(psutil.virtual_memory().percent)


def _root(base_url: str) -> str:
    """OpenAI 相容網址（…/v1）→ 伺服器根網址。"""
    url = base_url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _is_this_machine(url: str) -> bool:
    host = httpx.URL(url).host
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


@dataclass
class ManagedModel:
    key: str
    label: str
    approx_mb: int
    where: str  # 後端行程／Ollama／llama-server／JVM
    probe: Callable[[], bool | None]  # 是否載入中；None＝連不上或不在本機
    release: Callable[[], str]  # 回傳說明；失敗丟例外
    worth_releasing: Callable[[], bool] | None = None  # 有載入但不一定值得釋放（JVM 閒置時）


# ------------------------------------------------------------------ 各模型的偵測與釋放
def _embedder(name: str) -> tuple[Callable[[], bool | None], Callable[[], str]]:
    from app.rag import embedders

    return (lambda: embedders.is_loaded(name)), (
        lambda: "已卸載" if embedders.release(name) else "未載入"
    )


def _ollama_models() -> list[str]:
    s = get_settings()
    cfg = get_models_config().strategies
    names = {s.hybrid_model or cfg["hybrid"].default_model}
    if not s.hybrid_fallback_base_url or s.hybrid_fallback_base_url == s.hybrid_base_url:
        names.add(s.hybrid_fallback_model or cfg["hybrid_fallback"].default_model)
    return sorted(names)


def _ollama_probe() -> bool | None:
    url = get_settings().hybrid_base_url
    if not _is_this_machine(url):
        return None
    try:
        r = httpx.get(_root(url) + "/api/ps", timeout=1.0)
        loaded = {m["name"] for m in r.json().get("models", [])} | {
            m.get("model") for m in r.json().get("models", [])
        }
    except (httpx.HTTPError, ValueError):
        return None
    return any(n in loaded for n in _ollama_models())


def _ollama_release() -> str:
    root = _root(get_settings().hybrid_base_url)
    for name in _ollama_models():
        httpx.post(f"{root}/api/generate", json={"model": name, "keep_alive": 0}, timeout=10.0)
    for _ in range(20):  # Ollama 是非同步卸載，最多等 2 秒
        if not _ollama_probe():
            break
        time.sleep(0.1)
    return "keep_alive=0：" + "、".join(_ollama_models())


def _ortho_model() -> str:
    s = get_settings()
    return s.ortho2cad_model or get_models_config().strategies["ortho2cad"].default_model


def _ortho_probe() -> bool | None:
    url = get_settings().ortho2cad_base_url
    if not _is_this_machine(url):
        return None
    try:
        r = httpx.get(_root(url) + "/models", timeout=1.0)
        if r.status_code != 200:
            return None
        for m in r.json().get("data", []):
            if m.get("id") == _ortho_model():
                status = (m.get("status") or {}).get("value")
                # 沒有 status 欄位＝單一模型模式（舊的啟動方式），模型一直在記憶體裡
                return True if status is None else status in ("loaded", "loading")
    except (httpx.HTTPError, ValueError):
        return None
    return False


def _ortho_release() -> str:
    r = httpx.post(
        _root(get_settings().ortho2cad_base_url) + "/models/unload",
        json={"model": _ortho_model()},
        timeout=15.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            "llama-server 不是 router 模式，無法卸載（請用 make ortho2cad 重新啟動）"
        )
    for _ in range(50):  # 子行程結束要幾秒，等它真的卸載再量記憶體（最多 5 秒）
        if not _ortho_probe():
            break
        time.sleep(0.1)
    return "llama-server /models/unload"


# JVM 閒置時 heap 約 30 MB；超過這個量才值得 GC（否則記憶體一直高時每 5 秒都會「釋放」一次）
TIMEFOLD_IDLE_MB = 64


def _timefold_heap_mb() -> int | None:
    try:
        r = httpx.get(get_settings().scheduler_base_url.rstrip("/") + "/health", timeout=1.0)
        return r.json().get("memory", {}).get("committedMb", 0) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _timefold_probe() -> bool | None:
    return None if _timefold_heap_mb() is None else True


def _timefold_worth_releasing() -> bool:
    return (_timefold_heap_mb() or 0) > TIMEFOLD_IDLE_MB


def _timefold_release() -> str:
    r = httpx.post(get_settings().scheduler_base_url.rstrip("/") + "/admin/release", timeout=10.0)
    d = r.json()
    return f"GC：heap {d['before']['committedMb']}→{d['after']['committedMb']} MB"


def _models() -> list[ManagedModel]:
    clip, bge = _embedder("clip"), _embedder("bge")
    return [
        ManagedModel("clip", "Chinese-CLIP（以圖搜圖）", 750, "後端行程", *clip),
        ManagedModel("bge", "bge-m3（文字檢索）", 2200, "後端行程", *bge),
        ManagedModel(
            "qwen", "Qwen3-VL（問答、Text-to-SQL）", 4000, "Ollama", _ollama_probe, _ollama_release
        ),
        ManagedModel(
            "ortho2cad", "Ortho2CAD（3D 重建）", 6200, "llama-server", _ortho_probe, _ortho_release
        ),
        ManagedModel(
            "timefold",
            "Timefold 排程服務",
            200,
            "JVM",
            _timefold_probe,
            _timefold_release,
            _timefold_worth_releasing,
        ),  # fmt: skip
    ]


# ------------------------------------------------------------------ 狀態與觸發
class MemoryGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._release_lock = threading.Lock()
        self.in_use: dict[str, int] = {}
        self.active: list[str] = []  # 進行中的流程（巢狀時最外層是「目前流程」）
        self.current_flow: str | None = None
        self.current_models: set[str] = set()
        self.flow_at: str | None = None
        self.events: deque[dict] = deque(maxlen=30)
        self._last_noop = 0.0

    @property
    def threshold(self) -> float:
        return get_settings().memory_high_pct

    def enter(self, flow: str, models: set[str]) -> None:
        with self._lock:
            for k in models:
                self.in_use[k] = self.in_use.get(k, 0) + 1
            if flow != "manual" and not self.active:
                # 3D 重建裡呼叫圖紙辨識這類巢狀流程，不改變「目前流程」
                self.current_flow, self.current_models = flow, set(models)
                self.flow_at = datetime.now(UTC).isoformat()
            elif self.active and flow == self.current_flow:
                self.current_models |= set(models)
            self.active.append(flow)

    def exit(self, flow: str, models: set[str]) -> None:
        with self._lock:
            for k in models:
                self.in_use[k] = max(0, self.in_use.get(k, 0) - 1)
            if flow in self.active:
                self.active.remove(flow)

    def busy(self) -> set[str]:
        with self._lock:
            return {k for k, n in self.in_use.items() if n > 0}

    def projected_percent(self, before: float, keep: set[str]) -> tuple[float, list[str]]:
        """這個流程要用、但目前沒載入的模型載入後，記憶體大約會到幾 %（模型大小用估計值）。"""
        total = psutil.virtual_memory().total >> 20
        models = [m for m in _models() if m.key in keep]
        if before + sum(m.approx_mb for m in models) / total * 100 < self.threshold:
            return before, []  # 全部都要載入也不會超過門檻：不必逐一詢問
        missing = [m for m in models if m.probe() is False]
        return before + sum(m.approx_mb for m in missing) / total * 100, [m.label for m in missing]

    def check(
        self, trigger: str, keep: set[str], force: bool = False, anticipate: bool = False
    ) -> dict | None:
        """使用率超過門檻（或 force）就釋放 keep 與使用中以外、目前有載入的模型。

        anticipate=True（進入流程時）：還要載入的模型算進去，預估超過門檻就先釋放，
        避免載入後才超過、等背景監控才處理。回傳事件；沒有觸發時回 None。
        """
        s = get_settings()
        if not s.memory_guard and not force:
            return None
        before = memory_percent()
        projected, loading = self.projected_percent(before, keep) if anticipate else (before, [])
        if projected < self.threshold and not force:
            return None
        if loading and before < self.threshold:
            trigger += f"（預估載入 {'、'.join(loading)} 後約 {projected:.0f}%）"
        if not self._release_lock.acquire(blocking=False):
            return None  # 另一個請求正在釋放
        try:
            protect = set(keep) | self.busy()
            released, failed, kept = [], [], []
            for m in _models():
                loaded = m.probe()
                if not loaded:
                    continue
                if m.key not in protect and m.worth_releasing and not m.worth_releasing():
                    continue
                if m.key in protect:
                    kept.append(m.label)
                    continue
                try:
                    detail = m.release()
                    released.append(
                        {"key": m.key, "label": m.label, "detail": detail, "approx_mb": m.approx_mb}
                    )
                except Exception as e:  # noqa: BLE001 — 釋放失敗只記錄，不影響流程
                    failed.append({"key": m.key, "label": m.label, "detail": str(e)})
            if not released and not failed and not force:
                # 沒東西可放（只剩目前流程的模型）：一分鐘最多記一次，避免洗版
                if time.monotonic() - self._last_noop < 60:
                    return None
                self._last_noop = time.monotonic()
            if released:
                time.sleep(0.8)  # 給作業系統一點時間回收
            event = {
                "at": datetime.now(UTC).isoformat(),
                "trigger": trigger,
                "flow": self.current_flow,
                "flow_label": FLOWS.get(self.current_flow or "", ""),
                "threshold": self.threshold,
                "percent_before": round(before, 1),
                "percent_after": round(memory_percent(), 1),
                "released": released,
                "failed": failed,
                "kept": kept,
            }
            self.events.appendleft(event)
            log.info(
                "memory_guard",
                extra={
                    "fields": {
                        "trigger": trigger,
                        "before": event["percent_before"],
                        "after": event["percent_after"],
                        "released": [r["key"] for r in released],
                        "failed": [f["key"] for f in failed],
                    }
                },
            )
            return event
        finally:
            self._release_lock.release()

    def status(self) -> dict:
        vm = psutil.virtual_memory()
        busy = self.busy()
        models = []
        for m in _models():
            loaded = m.probe()
            models.append(
                {
                    "key": m.key,
                    "label": m.label,
                    "where": m.where,
                    "approx_mb": m.approx_mb,
                    "loaded": loaded,
                    "in_use": m.key in busy,
                    "needed_by_current_flow": m.key in self.current_models,
                }
            )
        return {
            "enabled": get_settings().memory_guard,
            "percent": round(vm.percent, 1),
            "threshold": self.threshold,
            "total_mb": vm.total >> 20,
            "available_mb": vm.available >> 20,
            "current_flow": self.current_flow,
            "current_flow_label": FLOWS.get(self.current_flow or ""),
            "flow_at": self.flow_at,
            "models": models,
            "events": list(self.events)[:10],
        }


guard = MemoryGuard()


@contextmanager
def use(flow: str, models: set[str]):
    """同步流程（搜尋）用：標記使用中的模型，記憶體超過門檻就釋放其他模型。"""
    guard.enter(flow, models)
    try:
        guard.check(f"進入「{FLOWS.get(flow, flow)}」", keep=models, anticipate=True)
        yield
    finally:
        guard.exit(flow, models)


def guarded(flow: str, models: set[str]):
    """同步函式的裝飾器版本：@guarded("search_image", {"clip"})。"""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with use(flow, models):
                return fn(*args, **kwargs)

        return wrapper

    return deco


@asynccontextmanager
async def flow(name: str, models: set[str]):
    """非同步流程（SSE）用；檢查與釋放在執行緒裡做，不擋住事件迴圈。"""
    guard.enter(name, models)
    try:
        await asyncio.to_thread(
            guard.check, f"進入「{FLOWS.get(name, name)}」", models, False, True
        )
        yield
    finally:
        guard.exit(name, models)


async def stream(name: str, models: set[str], events: AsyncIterator[str]) -> AsyncIterator[str]:
    """把 SSE 產生器包在流程裡：串流結束（或使用者離開頁面）時才解除「使用中」。"""
    async with flow(name, models), aclosing(events) as it:
        async for e in it:
            yield e


async def watch() -> None:
    """背景監控：每隔幾秒檢查一次，保留最近一次流程的模型。"""
    while True:
        await asyncio.sleep(get_settings().memory_check_interval_s)
        try:
            await asyncio.to_thread(guard.check, "背景監控", set(guard.current_models))
        except Exception as e:  # noqa: BLE001 — 監控失敗不能讓後端掛掉
            log.error(f"記憶體監控失敗：{e}")


def last_event_since(t0_iso: str) -> dict | None:
    """這次請求期間發生的釋放事件（給 SSE done 事件附帶顯示）。"""
    for e in guard.events:
        if e["at"] >= t0_iso:
            return e
    return None
