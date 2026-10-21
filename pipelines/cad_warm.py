"""預熱 3D 重建結果快取（make cad-warm，docs/adr/042）。

逐張知識庫零件（有標準模型的）：
1. 先跑一般重建（已經有合格的快取就直接命中、沙箱重跑後再過一次閘門；沒有就 greedy 生成）
2. greedy 不合格（IoU 低於 cad.cache.min_iou、執行失敗、實體無效、孔位校正失敗）就依序重抽
   seed 1…cad.resample.warm_seeds（temperature＝cad.resample.temperature），每張圖的重抽時間
   上限 cad.resample.time_limit_s；快取只在 IoU 更高時換掉，所以最後留下 IoU 最高且達門檻的那個
3. 沒有標準模型的零件（照片建檔）不自動寫入，要主管在畫面上按「採用這個結果」

執行時像 worker 一樣在 Redis 登記 ortho2cad 使用中（memory_guard），不會被別的行程判斷記憶體
吃緊而卸載。需要 Ortho2CAD（make ortho2cad）與 LLM_MODE 不是 mock。

    make cad-warm                     # 全部零件
    make cad-warm PARTS="mfg-001 mfg-005"

退出碼：每張有標準模型的零件都有合格快取（命中或寫入）才是 0；有任何一張失敗、沒有合格候選
或模型身分不明回 1；設定不能預熱（mock 模式、找不到模型檔、圖紙不存在）回 2。
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.jobs.sse_util import parse  # noqa: E402
from app.repositories.index_store import get_store  # noqa: E402
from app.services import cad_cache, cad_service, memory_guard  # noqa: E402

RENEW_S = 30  # 模型使用登記的續約間隔（登記存活時間預設 120 秒）
# 算預熱成功的結果：有合格快取（命中或剛寫入），或沒有標準模型（預期略過，要主管採用）。
# error（模型、沙箱失敗）、not_stored（每個候選都不合格）、not_cacheable（模型身分不明）都回非零
OK_STATUSES = {"hit", "stored", "no_gt"}


class UsageRenewal(threading.Thread):
    """生成一次要好幾分鐘：定期續約 Redis 裡「本行程使用中的模型」登記
    （worker 靠維護執行緒做同一件事）。"""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="cad-warm-usage")
        self.stop_evt = threading.Event()

    def run(self) -> None:
        while not self.stop_evt.wait(RENEW_S):
            try:
                memory_guard.guard.publish()
            except Exception as e:  # noqa: BLE001 — 續約失敗下一輪再試
                print(f"  （模型使用登記續約失敗：{e}）", flush=True)


async def run_once(part_id: str, candidate: cad_cache.Candidate | None = None) -> dict:
    """跑一次 3D 重建（和背景工作同一條管線），回 {"result": …} 或 {"error": …}。"""
    out = Path(tempfile.mkdtemp(prefix="cad-warm-"))
    try:
        events = cad_service._reconstruct_stream(
            request_id="warm_" + uuid.uuid4().hex[:8],
            part_id=part_id,
            strategy="ortho2cad",
            account=None,  # 系統預熱：不限資料範圍
            out_dir=out,
            file_url=lambda _job, _name: "",
            include_code=False,
            candidate=candidate,
            cache_source="warm",
        )
        result, done = None, None
        async for raw in memory_guard.stream("reconstruct", {"ortho2cad"}, events):
            name, data = parse(raw)
            if name == "result":
                result = data
            elif name == "done":
                done = data
            elif name == "error":
                return {"error": data}
        if result is None:
            return {"error": {"code": "CAD_NO_RESULT", "message": "沒有產出結果"}}
        return {"result": result, "done": done}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def describe(c: dict | None) -> str:
    if not c:
        return "—"
    if c.get("kind") == "greedy":
        return "greedy"
    return f"temperature {c.get('temperature')}・seed {c.get('seed')}"


def line(label: str, r: dict) -> str:
    if "error" in r:
        return f"  {label:<28} 失敗：{r['error'].get('code')} {r['error'].get('message')}"
    res, cache = r["result"], r["result"].get("cache") or {}
    iou = res.get("iou")
    total = (r.get("done") or {}).get("latency_ms", {}).get("total")
    parts = [
        f"  {label:<28}",
        f"IoU {iou:.3f}" if isinstance(iou, (int, float)) else "IoU —",
        f"快取 {cache.get('status', '—')}",
        f"{total / 1000:.1f} 秒" if total else "",
        f"（{cache['reason']}）" if cache.get("reason") else "",
    ]
    return " ".join(p for p in parts if p)


async def warm_part(part: dict, rs: cad_cache.ResampleSpec) -> dict:
    pid = part["id"]
    print(f"[{pid}] {part.get('name', {}).get('zh', '')}", flush=True)
    first = await run_once(pid)
    print(line("一般重建（快取或 greedy）", first), flush=True)
    tries = [("greedy", first)]
    cache = first.get("result", {}).get("cache") if "result" in first else None
    if "error" in first:
        return {"part_id": pid, "status": "error", "tries": tries}
    if cache is None:
        return {"part_id": pid, "status": "not_cacheable", "tries": tries}
    if cache["status"] in ("hit", "stored"):
        return {"part_id": pid, "status": cache["status"], "tries": tries}
    t0 = time.monotonic()
    for seed in range(1, rs.warm_seeds + 1):
        if time.monotonic() - t0 > rs.time_limit_s:
            print(f"  已超過重抽時間上限 {rs.time_limit_s:.0f} 秒，停止", flush=True)
            break
        cand = cad_cache.Candidate(rs.temperature, seed)
        r = await run_once(pid, cand)
        print(line(f"重抽 {describe(cand.to_dict())}", r), flush=True)
        tries.append((describe(cand.to_dict()), r))
    stored = [
        r["result"]
        for _, r in tries
        if "result" in r and (r["result"].get("cache") or {}).get("status") == "stored"
    ]
    if not stored:
        return {"part_id": pid, "status": "not_stored", "tries": tries}
    best = max(stored, key=lambda x: x.get("iou") or 0)
    return {"part_id": pid, "status": "stored", "best": best["cache"], "tries": tries}


async def main_async(part_ids: list[str]) -> int:
    s = get_settings()
    if s.llm_mode == "mock":
        print("LLM_MODE=mock：沒有真的模型，不預熱快取", file=sys.stderr)
        return 2
    store = get_store()
    store.load()
    alias = s.ortho2cad_model or "ortho2cad"
    if cad_cache.model_identity(alias) is None:
        print(
            f"找不到 deploy/llama-router.ini 裡 [{alias}] 的模型檔：模型身分不明，不預熱快取",
            file=sys.stderr,
        )
        return 2
    parts = [p for p in store.parts if not part_ids or p["id"] in part_ids]
    missing = set(part_ids) - {p["id"] for p in parts}
    if missing:
        print(f"找不到圖紙：{', '.join(sorted(missing))}", file=sys.stderr)
        return 2
    rs = cad_cache.resample_spec()
    print(
        f"門檻 IoU ≥ {cad_cache.cache_spec().min_iou}；greedy 不合格時重抽 seed 1…{rs.warm_seeds}"
        f"（temperature {rs.temperature}，每張上限 {rs.time_limit_s:.0f} 秒）",
        flush=True,
    )
    renewal = UsageRenewal()
    renewal.start()
    report = []
    try:
        for part in parts:
            if not (store.parts_dir / "gt" / f"{part['id']}.step").exists():
                print(f"[{part['id']}] 沒有標準模型，不自動寫入（要主管按「採用這個結果」）")
                report.append({"part_id": part["id"], "status": "no_gt"})
                continue
            report.append(await warm_part(part, rs))
    finally:
        renewal.stop_evt.set()
    print("\n結果：")
    for r in report:
        best = r.get("best")
        extra = (
            f"（IoU {best['iou']:.3f}，{describe(best['candidate'])}）"
            if best and best.get("iou") is not None
            else ""
        )
        print(f"  {r['part_id']}: {r['status']}{extra}")
    failed = [r["part_id"] for r in report if r["status"] not in OK_STATUSES]
    if failed:
        # 部署腳本、操作者看退出碼判斷預熱完成：有標準模型的零件沒拿到合格快取就不算成功
        print(f"\n預熱沒有完成：{', '.join(failed)} 沒有合格的快取", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("parts", nargs="*", help="只預熱這些圖紙（預設全部）")
    args = ap.parse_args()
    return asyncio.run(main_async(args.parts))


if __name__ == "__main__":
    sys.exit(main())
