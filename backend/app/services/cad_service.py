"""工廠圖紙 → 3D 模型（Ortho2CAD）的流程編排，以 SSE 串流回傳。

1. 取得圖紙：知識庫圖紙直接用；照片先辨識，對上知識庫就依 homography 拉正，否則當作未收錄圖紙
2. 尺寸依據：知識庫圖紙用標註尺寸；未收錄圖紙請本地 Qwen3-VL 讀圖上的三個外形尺寸
   （Ortho2CAD 輸出的是 DeepCAD 正規化尺度，約 0–1，要等比放大回 mm）
3. Ortho2CAD 生成 CadQuery 程式碼（逐字串流）
4. 沙箱執行 → STL／STEP／回投影三視圖；知識庫零件另與標準模型比 IoU（論文的評估法）
   有標準三視圖的零件，執行前把模型畫的垂直圓孔換成 OpenCV 在俯視圖找到的孔（docs/adr/035）
結果快取（docs/adr/042）：知識庫圖紙＋Ortho2CAD 在第 3 步之前先查快取（services/cad_cache.py），
命中就跳過生成；合格（執行成功、實體有效、孔位校正沒失敗、IoU 達門檻）的結果才寫入。

全程只連本機或內網的推論伺服器，外送資料量恆為 0。strategy="hybrid" 是對照組：
同一張圖改給未微調的 Qwen3-VL 產生程式碼，用來展示領域微調的效果。
"""

import asyncio
import base64
import io
import json
import re
import secrets
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from app.cad.holes import DETECT_FAILED_REASON, hole_plan
from app.cad.preprocess import model_input
from app.cad.sandbox import WslTarget, extract_code, run_cad
from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import cad_access, cad_cache, memory_guard, uploads
from app.services.cad_access import JOB_FILES
from app.services.chat_service import NO_EGRESS, sse
from app.services.identity import Account, require_part, visible_part_ids
from app.services.search_service import identify_drawing, load_upload, part_summary, rectify_to_part
from app.services.security import CadAccess

CAD_STRATEGIES = {"ortho2cad", "hybrid"}

DIMS_PROMPT = (
    "這是一張機械零件的三視圖（第一角法）。圖上只有三個外形尺寸標註：\n"
    "- 俯視圖（Top View）正下方的水平尺寸＝寬度 width\n"
    "- 俯視圖（Top View）右側的垂直尺寸＝深度 depth\n"
    "- 前視圖（Front View）右側的垂直尺寸＝高度 height\n"
    '請讀出這三個數字，只輸出一行 JSON，例如 {"width": 80.0, "depth": 50.0, "height": 60.0}；'
    "讀不到的填 null。"
)
MOCK_CODE = 'import cadquery as cq\nsolid = cq.Workplane("XY").box(1.0, 0.6, 0.4)\n'


def wsl_target() -> WslTarget | None:
    """.env 有設 CAD_WSL_PYTHON：Windows 主機在 WSL2 執行模型產生的程式碼（docs/adr/027）。"""
    s = get_settings()
    return WslTarget(s.cad_wsl_python, s.cad_wsl_distro) if s.cad_wsl_python else None


def _image_part(img: Image.Image, fmt: str = "PNG") -> dict:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    mime = "png" if fmt == "PNG" else "jpeg"
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}}


async def read_dimensions(img: Image.Image) -> dict | None:
    """請本地 Qwen3-VL 讀圖紙上的外形尺寸（寬、深、高，mm）；讀不到就回 None。"""
    if get_settings().llm_mode == "mock":
        return None
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
         + base64.b64encode(to_jpeg_bytes(img, 90)).decode()}},
        {"type": "text", "text": DIMS_PROMPT},
    ]  # fmt: skip
    for strategy in ("hybrid", "hybrid_fallback"):
        try:
            provider = get_provider(strategy)
            provider.max_tokens, provider.temperature = 120, 0
            text = "".join(
                [p async for p in provider.stream([{"role": "user", "content": content}])]
            )
            m = re.search(r"\{.*?\}", text, flags=re.S)
            dims = json.loads(m.group(0)) if m else {}
            vals = {k: dims.get(k) for k in ("width", "depth", "height")}
            if all(isinstance(v, (int, float)) and v > 0 for v in vals.values()):
                return {k: float(v) for k, v in vals.items()}
            return None
        except (ProviderUnavailable, json.JSONDecodeError):
            continue
    return None


HOLE_LAYOUTS = ("kb", "rectified")  # 圖紙版面是標準三視圖的才校正；upload＝照片沒拉正，版面不固定


def hole_plan_for(part: dict | None, scale_to: dict | None, layout: str):
    """孔位校正（docs/adr/035）用哪張圖：有標準模型的知識庫零件用它的標準三視圖
    （照片拉正後辨識出來的也一樣，不用照片）；回 (孔位, None) 或 (None, 不校正的原因)。
    layout 是 3D 重建實際用的圖紙版面：照片沒拉正（upload）時，即使辨識出知識庫零件也不校正。"""
    if part is None:
        return None, "未收錄的圖紙版面不固定，孔位維持模型輸出"
    if layout not in HOLE_LAYOUTS:
        return None, f"照片沒有拉正成圖紙版面（{layout}），孔位維持模型輸出"
    if "cad" not in part:
        return None, "照片建檔的圖紙不是標準三視圖版面，孔位維持模型輸出"
    try:
        return hole_plan(Image.open(REPO_ROOT / part["drawing"]), scale_to)
    except Exception as e:  # 偵測失敗不影響重建，只是不校正
        log.warning("cad_holes_failed", extra={"fields": {"part_id": part["id"], "error": str(e)}})
        return None, DETECT_FAILED_REASON


async def _mock_stream(code: str) -> AsyncIterator[str]:
    for i in range(0, len(code), 6):
        await asyncio.sleep(0.01)
        yield code[i : i + 6]


OUTPUT_FILES = ("model.stl", "model.step", "reproj.png", "result.json")


def _clear_outputs(job_dir: Path) -> None:
    """快取命中重跑後沒過閘門、改由模型重新產生之前：清掉那次執行的產物，失敗時不會留下舊模型。"""
    for name in OUTPUT_FILES:
        (job_dir / name).unlink(missing_ok=True)


def purge_jobs(ttl_days: int) -> int:
    """刪掉超過保存期限的 3D 重建結果（與上傳照片同一個期限）。"""
    root = get_settings().cad_jobs_dir
    if not root.is_dir():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).timestamp()
    n = 0
    for d in root.iterdir():
        # .staging 是背景工作寫到一半的暫存資料夾（成功才改名發布），由工作系統自己清
        if d.is_dir() and not d.name.startswith(".") and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


async def reconstruct_stream(
    request_id: str,
    part_id: str | None = None,
    image_id: str | None = None,
    strategy: str = "ortho2cad",
    account: Account | None = None,
) -> AsyncIterator[str]:
    """3D 重建用到 Ortho2CAD（對照組改用 Qwen3-VL）；照片要先辨識（Chinese-CLIP），
    未收錄圖紙要請 Qwen3-VL 讀尺寸。記憶體吃緊時先釋放其他模型。
    account：照片辨識出知識庫圖紙時，檢查目前身分看不看得到（資料範圍）；None＝不限。"""
    models = {"ortho2cad" if strategy == "ortho2cad" else "qwen"}
    if image_id:
        models |= {"clip", "qwen"}
    events = _reconstruct_stream(request_id, part_id, image_id, strategy, account)
    async for e in memory_guard.stream("reconstruct", models, events):
        yield e


def public_error(text: str | None, limit: int = 200) -> str | None:
    """進度、事件與工作結果裡的錯誤只留第一行、限長：沙箱的錯誤訊息可能夾帶模型產生的程式碼或路徑
    （授權人員要看程式碼，走受保護的 code.py 下載端點）。"""
    if not text:
        return text
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:limit]


async def _reconstruct_stream(
    request_id: str,
    part_id: str | None = None,
    image_id: str | None = None,
    strategy: str = "ortho2cad",
    account: Account | None = None,
    *,
    job_id: str | None = None,
    out_dir: Path | None = None,
    file_url: Callable[[str, str], str] | None = None,
    include_code: bool = True,
    checkpoint: Callable[[str], None] | None = None,
    regenerate: bool = False,
    candidate: cad_cache.Candidate | None = None,
    cache_source: str = "auto",
) -> AsyncIterator[str]:
    """job_id／out_dir／file_url／include_code／checkpoint 是背景工作用的（services/cad_job.py）：
    工作編號與暫存資料夾由工作系統給、檔案網址指向受保護的產物端點、事件不帶程式碼、
    階段之間檢查有沒有被取消或撤權。既有的 SSE 入口不傳這些，行為不變。

    結果快取（docs/adr/042）：知識庫圖紙＋Ortho2CAD 先查快取，命中就跳過生成、程式碼照樣經過
    安全檢查與沙箱，重跑後再過一次閘門。regenerate＝「結果不對，重新生成」：不查快取，
    用 temperature＋下一個沒試過的 seed 產生一個新候選。candidate、cache_source 給 make cad-warm
    指定候選與寫入來源。檢查圖紙權限（require_part）在查快取之前。"""
    t0 = time.perf_counter()
    check = checkpoint or (lambda _stage: None)
    s, cfg, store = get_settings(), get_models_config().cad, get_store()

    def err(code: str, message: str) -> str:
        return sse("error", {"code": code, "request_id": request_id, "message": message})

    if strategy not in CAD_STRATEGIES:
        yield err("STRATEGY_UNAVAILABLE", f"3D 重建不支援策略 {strategy}")
        return

    # 1. 取得圖紙與版面
    check("identify")
    part, identified, photo = None, None, None
    if part_id:
        part = store.get_part(part_id)
        if not part:
            yield err("PART_NOT_FOUND", f"找不到圖紙 {part_id}")
            return
    if image_id:
        photo = load_image(load_upload(image_id, account))
        if not part:
            # 只在看得到的圖紙裡辨識（docs/adr/030）：看不到的圖紙不讀，認不出就當未收錄的照片
            visible = visible_part_ids(account, store.parts) if account is not None else None
            identified = await asyncio.to_thread(
                identify_drawing, image_id, None, photo, None, visible
            )
            if identified["matched"]:
                part = store.get_part(identified["best_part_id"])
    if not part and not photo:
        yield err("VALIDATION_ERROR", "請指定 part_id 或 image_id")
        return
    if part and account is not None:
        try:
            require_part(account, part)
        except AppError as e:
            yield err(e.code, e.message)
            return
    if photo is None:
        drawing, layout = Image.open(REPO_ROOT / part["drawing"]).convert("RGB"), "kb"
    else:
        rect = await asyncio.to_thread(rectify_to_part, photo, part) if part else None
        drawing, layout = (rect, "rectified") if rect is not None else (photo, "upload")
    max_pixels = int(cfg["max_pixels"]) if strategy == "ortho2cad" else 800 * 800
    model_img = await asyncio.to_thread(model_input, drawing, layout, max_pixels)

    job_id = job_id or "cad_" + uuid.uuid4().hex[:16]
    job_dir = out_dir or s.cad_jobs_dir / job_id
    if out_dir is None:
        # 既有的 SSE 入口：資料夾建好後第一個寫入的檔案就是存取紀錄，之後不管跑到哪一步
        # （未完成、失敗、完成），下載端點都靠它授權，不看 summary.json 在不在
        cad_access.write_access(
            job_id,
            CadAccess(
                job_id,
                account.id if account else None,
                part_id,
                image_id,
                identified["best_part_id"] if identified and identified["matched"] else None,
                source_owner_id=uploads.owner_of(image_id) if image_id else None,
            ),
            job_dir,
        )
    job_dir.mkdir(parents=True, exist_ok=True)
    model_img.save(job_dir / "input.png")
    url_of = file_url or (lambda jid, name: f"/api/v1/cad/jobs/{jid}/{name}")
    files = {name: url_of(job_id, name) for name in sorted(JOB_FILES)}

    # 2. 尺寸依據
    if part:
        g = part["geometry"]
        scale_to = {k: g[k] for k in ("width", "depth", "height")}
        # 照片建檔的零件沒有標準模型（docs/adr/013）：外形是建檔時人確認過的圖上標註
        scale_source = "知識庫圖紙標註尺寸" if "cad" in part else "照片建檔時確認的圖上標註"
    else:
        scale_to = await read_dimensions(photo)
        scale_source = "Qwen3-VL 讀取圖上標註" if scale_to else None

    try:
        provider = None if s.llm_mode == "mock" else get_provider(strategy)
    except ProviderUnavailable as e:
        yield err("STRATEGY_UNAVAILABLE", str(e))
        return
    model = provider.model if provider else "mock-ground-truth"
    meta = {
        "request_id": request_id,
        "job_id": job_id,
        "strategy": strategy,
        "model": model,
        "image_id": image_id,
        "part": part_summary(part) if part else None,
        "identified": identified,
        "layout": layout,
        "input_url": files["input.png"],
        "input_size": list(model_img.size),
        "scale_to": scale_to,
        "scale_source": scale_source,
    }
    yield sse("meta", meta)

    prompt = str(cfg["prompt"]) + (str(cfg["baseline_suffix"]) if strategy != "ortho2cad" else "")
    gt_path = store.parts_dir / "gt" / f"{part['id']}.step" if part else None
    gt_step = gt_path if gt_path and gt_path.exists() else None
    holes, hole_reason = await asyncio.to_thread(hole_plan_for, part, scale_to, layout)
    cctx = await asyncio.to_thread(
        cad_cache.context,
        strategy=strategy,
        layout=layout,
        part=part,
        model_alias=provider.model if provider else None,
        input_png=(job_dir / "input.png").read_bytes(),
        prompt=prompt,
        max_pixels=max_pixels,
        max_tokens=int(cfg["max_tokens"]),
        has_gt=gt_step is not None,
    )

    async def execute(code: str):
        return await run_cad(
            code,
            job_dir,
            scale_to=scale_to,
            holes=holes,
            gt_step=gt_step,
            timeout_s=float(cfg["exec_timeout_s"]),
            wsl=wsl_target(),
        )

    def result_of(run, code: str) -> dict:
        d = run.data
        return {
            "ok": run.ok,
            "error": run.error if include_code else public_error(run.error),
            **({"code": code} if include_code else {}),
            "valid": d.get("valid"),
            "repaired": d.get("repaired"),
            "raw_dims": d.get("raw_dims"),
            "dims": d.get("dims"),
            "scale": d.get("scale"),
            "volume": d.get("volume"),
            "faces": d.get("faces"),
            "iou": d.get("iou"),
            "iou_bbox": d.get("iou_bbox"),
            "holes": d.get("holes") or {"status": "skipped", "reason": hole_reason, "detected": 0},
            "files": files
            if run.ok
            else {"code.py": files["code.py"], "input.png": files["input.png"]},
        }

    # 3. 結果快取（docs/adr/042）：命中就不生成，程式碼照樣經過安全檢查與沙箱，重跑後再過一次閘門
    run, result, cache_info, code, answer = None, None, None, "", ""
    first_token_ms, generation_ms = None, 0
    hit = None
    if cctx is not None and not regenerate and candidate is None:
        hit = await asyncio.to_thread(cctx.lookup)
    if hit is not None:
        check("execute")
        code = hit.code
        (job_dir / "code.py").write_text(code, encoding="utf-8")
        yield sse("executing", {"generation_ms": 0, "code_chars": len(code), "cached": True})
        run = await execute(code)
        result = result_of(run, code)
        ok, why = cad_cache.regate(result, has_gt=cctx.has_gt)
        if ok:
            cache_info = await asyncio.to_thread(cad_cache.after_hit, cctx, job_id, hit, result)
        else:
            # 門檻調高、環境變了：移除這一筆，當成沒命中，改由模型重新產生
            await asyncio.to_thread(cad_cache.get_cache().remove, cctx.key, hit.code_sha)
            log.info(
                "cad_cache_evicted",
                extra={"fields": {"part_id": cctx.part_id, "reason": why}},
            )
            _clear_outputs(job_dir)
            run = result = None

    # 4. 生成 CadQuery 程式碼（逐字串流）
    if run is None:
        check("generate")
        cand = candidate
        if cand is None and regenerate:
            rs = cad_cache.resample_spec()
            if cctx is not None:
                seed = await asyncio.to_thread(
                    cad_cache.get_cache().next_seed, cctx.key, rs.max_seeds
                )
                if seed is None:
                    yield err(
                        "CAD_RESAMPLE_EXHAUSTED",
                        f"這張圖紙已經試過 {rs.max_seeds} 個候選，沒有新的可以重新生成",
                    )
                    return
            else:
                seed = secrets.randbelow(2**31 - 2) + 1
            cand = cad_cache.Candidate(rs.temperature, seed)
        cand = cand or cad_cache.Candidate(float(cfg["temperature"]))
        messages = [
            {
                "role": "user",
                "content": [_image_part(model_img), {"type": "text", "text": prompt}],
            }
        ]
        if provider:
            provider.max_tokens = int(cfg["max_tokens"])
            provider.temperature = cand.temperature
            provider.seed = cand.seed
            pieces = provider.stream(messages)
        else:
            has_gt = part is not None and "cad" in part
            gt = (REPO_ROOT / part["cad"]).read_text(encoding="utf-8") if has_gt else MOCK_CODE
            pieces = _mock_stream(gt)
        t_gen = time.perf_counter()
        try:
            async for piece in pieces:
                check("generate")
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - t0) * 1000)
                answer += piece
                yield sse("token", {"text": piece})
        except ProviderUnavailable as e:
            hint = (
                "Ortho2CAD 推論伺服器無法使用，請確認已執行 make ortho2cad"
                if strategy == "ortho2cad"
                else "本地 VLM（Ollama）無法使用"
            )
            yield err("STRATEGY_UNAVAILABLE", f"{hint}（{e}）；3D 重建不改走雲端。")
            return
        generation_ms = round((time.perf_counter() - t_gen) * 1000)
        if cctx is not None and cand.seed is not None:
            await asyncio.to_thread(cad_cache.get_cache().mark_tried, cctx.key, cand.seed)

        # 5. 沙箱執行
        check("execute")
        code = extract_code(answer)
        (job_dir / "code.py").write_text(code, encoding="utf-8")  # 失敗（含安全檢查沒過）也留得住
        yield sse("executing", {"generation_ms": generation_ms, "code_chars": len(code)})
        run = await execute(code)
        result = result_of(run, code)
        check("write")
        if cctx is not None:
            cache_info = await asyncio.to_thread(
                cad_cache.after_run,
                cctx,
                job_id=job_id,
                code=code,
                candidate=cand,
                result=result,
                source=cache_source,
            )
    else:
        check("write")
    d = run.data
    if cache_info is not None:
        result["cache"] = cache_info
    yield sse("result", result)

    # 6. 完成
    total_ms = round((time.perf_counter() - t0) * 1000)
    out_tokens = provider.usage.output_tokens if provider else len(answer) // 4
    done = {
        "request_id": request_id,
        "job_id": job_id,
        "strategy": strategy,
        "model": model,
        "latency_ms": {
            "first_token": first_token_ms,
            "generation": generation_ms,
            "exec": d.get("total_ms"),
            "total": total_ms,
        },
        "tokens": {"input": provider.usage.input_tokens if provider else 0, "output": out_tokens},
        "egress": NO_EGRESS,
    }
    yield sse("done", done)
    (job_dir / "summary.json").write_text(
        json.dumps({"meta": meta, "result": result, "done": done}, ensure_ascii=False),
        encoding="utf-8",
    )
    get_logs_repo().add_cad_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "job_id": job_id,
            "part_id": part["id"] if part else None,
            "image_id": image_id,
            "strategy": strategy,
            "model": model,
            "ok": int(run.ok),
            "error": run.error,
            "iou": d.get("iou"),
            "iou_bbox": d.get("iou_bbox"),
            "scale": d.get("scale"),
            "scale_source": scale_source,
            "first_token_ms": first_token_ms,
            "generation_ms": generation_ms,
            "exec_ms": d.get("total_ms"),
            "total_ms": total_ms,
            "output_tokens": out_tokens,
        }
    )
    log.info(
        "cad",
        extra={
            "fields": {
                "request_id": request_id,
                "job_id": job_id,
                "strategy": strategy,
                "part_id": part["id"] if part else None,
                "ok": run.ok,
                "iou": d.get("iou"),
                "latency_ms": done["latency_ms"],
            }
        },
    )
