"""工廠圖紙 → 3D 模型（Ortho2CAD）的流程編排，以 SSE 串流回傳。

1. 取得圖紙：知識庫圖紙直接用；照片先辨識，對上知識庫就依 homography 拉正，否則當作未收錄圖紙
2. 尺寸依據：知識庫圖紙用標註尺寸；未收錄圖紙請本地 Qwen3-VL 讀圖上的三個外形尺寸
   （Ortho2CAD 輸出的是 DeepCAD 正規化尺度，約 0–1，要等比放大回 mm）
3. Ortho2CAD 生成 CadQuery 程式碼（逐字串流）
4. 沙箱執行 → STL／STEP／回投影三視圖；知識庫零件另與標準模型比 IoU（論文的評估法）

全程只連本機或內網的推論伺服器，外送資料量恆為 0。strategy="hybrid" 是對照組：
同一張圖改給未微調的 Qwen3-VL 產生程式碼，用來展示領域微調的效果。
"""

import asyncio
import base64
import io
import json
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from PIL import Image

from app.cad.preprocess import model_input
from app.cad.sandbox import extract_code, run_cad
from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import memory_guard
from app.services.chat_service import NO_EGRESS, sse
from app.services.identity import Account, require_part
from app.services.search_service import identify_drawing, load_upload, part_summary, rectify_to_part

CAD_STRATEGIES = {"ortho2cad", "hybrid"}
JOB_FILES = {"model.stl", "model.step", "reproj.png", "code.py", "input.png"}
JOB_ID = re.compile(r"^cad_[0-9a-f]{16}$")

DIMS_PROMPT = (
    "這是一張機械零件的三視圖（第一角法）。圖上只有三個外形尺寸標註：\n"
    "- 俯視圖（Top View）正下方的水平尺寸＝寬度 width\n"
    "- 俯視圖（Top View）右側的垂直尺寸＝深度 depth\n"
    "- 前視圖（Front View）右側的垂直尺寸＝高度 height\n"
    '請讀出這三個數字，只輸出一行 JSON，例如 {"width": 80.0, "depth": 50.0, "height": 60.0}；'
    "讀不到的填 null。"
)
MOCK_CODE = 'import cadquery as cq\nsolid = cq.Workplane("XY").box(1.0, 0.6, 0.4)\n'


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


async def _mock_stream(code: str) -> AsyncIterator[str]:
    for i in range(0, len(code), 6):
        await asyncio.sleep(0.01)
        yield code[i : i + 6]


def purge_jobs(ttl_days: int) -> int:
    """刪掉超過保存期限的 3D 重建結果（與上傳照片同一個期限）。"""
    root = get_settings().cad_jobs_dir
    if not root.is_dir():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).timestamp()
    n = 0
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
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


async def _reconstruct_stream(
    request_id: str,
    part_id: str | None = None,
    image_id: str | None = None,
    strategy: str = "ortho2cad",
    account: Account | None = None,
) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    s, cfg, store = get_settings(), get_models_config().cad, get_store()

    def err(code: str, message: str) -> str:
        return sse("error", {"code": code, "request_id": request_id, "message": message})

    if strategy not in CAD_STRATEGIES:
        yield err("STRATEGY_UNAVAILABLE", f"3D 重建不支援策略 {strategy}")
        return

    # 1. 取得圖紙與版面
    part, identified, photo = None, None, None
    if part_id:
        part = store.get_part(part_id)
        if not part:
            yield err("PART_NOT_FOUND", f"找不到圖紙 {part_id}")
            return
    if image_id:
        photo = load_image(load_upload(image_id))
        if not part:
            identified = await asyncio.to_thread(identify_drawing, image_id, None, photo)
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

    job_id = "cad_" + uuid.uuid4().hex[:16]
    job_dir = s.cad_jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    model_img.save(job_dir / "input.png")
    files = {name: f"/api/v1/cad/jobs/{job_id}/{name}" for name in sorted(JOB_FILES)}

    # 2. 尺寸依據
    if part:
        g = part["geometry"]
        scale_to = {k: g[k] for k in ("width", "depth", "height")}
        scale_source = "知識庫圖紙標註尺寸"
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

    # 3. 生成 CadQuery 程式碼（逐字串流）
    prompt = str(cfg["prompt"]) + (str(cfg["baseline_suffix"]) if strategy != "ortho2cad" else "")
    messages = [
        {
            "role": "user",
            "content": [_image_part(model_img), {"type": "text", "text": prompt}],
        }
    ]
    if provider:
        provider.max_tokens = int(cfg["max_tokens"])
        provider.temperature = float(cfg["temperature"])
        pieces = provider.stream(messages)
    else:
        gt = (REPO_ROOT / part["cad"]).read_text(encoding="utf-8") if part else MOCK_CODE
        pieces = _mock_stream(gt)
    t_gen = time.perf_counter()
    answer, first_token_ms = "", None
    try:
        async for piece in pieces:
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

    # 4. 沙箱執行
    code = extract_code(answer)
    yield sse("executing", {"generation_ms": generation_ms, "code_chars": len(code)})
    gt_step = store.parts_dir / "gt" / f"{part['id']}.step" if part else None
    run = await run_cad(
        code,
        job_dir,
        scale_to=scale_to,
        gt_step=gt_step if gt_step and gt_step.exists() else None,
        timeout_s=float(cfg["exec_timeout_s"]),
    )
    d = run.data
    result = {
        "ok": run.ok,
        "error": run.error,
        "code": code,
        "valid": d.get("valid"),
        "repaired": d.get("repaired"),
        "raw_dims": d.get("raw_dims"),
        "dims": d.get("dims"),
        "scale": d.get("scale"),
        "volume": d.get("volume"),
        "faces": d.get("faces"),
        "iou": d.get("iou"),
        "iou_bbox": d.get("iou_bbox"),
        "files": files
        if run.ok
        else {"code.py": files["code.py"], "input.png": files["input.png"]},
    }
    yield sse("result", result)

    # 5. 完成
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
