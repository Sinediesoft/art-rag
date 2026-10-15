"""系統：健康檢查、狀態、記憶體、評估紀錄、診斷、拒絕並記錄與稽核。"""

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.agent import jev
from app.api import schemas as S
from app.core.config import (
    REPO_ROOT,
    get_agent_config,
    get_models_config,
    get_settings,
    kb_version,
)
from app.jobs import boot as jobs_boot
from app.rag import conflict_check, providers, rearrange
from app.rag.kb import current_kb_hash
from app.repositories.index_store import get_store
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.logs_repo import get_logs_repo
from app.repositories.production_repo import get_production_repo
from app.services import (
    cad_cache,
    chat_service,
    identity,
    memory_guard,
    schedule_service,
)

router = APIRouter()


# ---------------------------------------------------------------- 記憶體管理
@router.get("/memory", response_model=S.MemoryStatus, tags=["system"])
def memory_status():
    """系統記憶體使用率、各模型是否載入／使用中、最近的釋放紀錄。"""
    return memory_guard.guard.status()


@router.post("/admin/memory/release", response_model=S.MemoryReleaseResponse, tags=["system"])
def release_memory(request: Request):
    """展示用：不管使用率，立刻釋放目前流程與其他請求用不到的模型（只在展示模式、本機）。"""
    identity.require_demo(request, "手動釋放模型")
    g = memory_guard.guard
    event = g.check("手動釋放", set(g.current_models), force=True)
    return {"event": event, "status": g.status()}


@router.get("/eval/runs", response_model=S.EvalRunsResponse, tags=["eval"])
def eval_runs():
    runs = []
    for p in sorted((REPO_ROOT / "eval" / "runs").glob("*.json"), reverse=True):
        # 圖紙、領域路由、Text-to-SQL、色彩分析、展示測試、智慧助理路由（-route）、
        # 五段防護第 2 段（-guard）、影像比對（-align）、照片建檔（-intake）、
        # 畫作卡推測（-style、線性分類頭的訓練報告 -style-head）
        # 與以圖搜圖同系列不同版本（-versions）、觀眾實拍照（-met-photos）、
        # 圖紙文字與照片檢索（-drawing-retrieval）的評估另有格式
        if not p.name.endswith(
            (
                "-drawing-retrieval.json",
                "-cad.json",
                "-router.json",
                "-sql.json",
                "-color.json",
                "-demo.json",
                "-route.json",
                "-guard.json",
                "-align.json",
                "-intake.json",
                "-style.json",
                "-style-head.json",
                "-versions.json",
                "-met-photos.json",
            )
        ):
            runs.append(json.loads(p.read_text(encoding="utf-8")))
    return {"runs": runs}


@router.get("/eval/cad-runs", response_model=S.CadEvalRunsResponse, tags=["eval"])
def cad_eval_runs():
    """工廠圖紙評估（make eval-cad）：圖紙辨識與 Ortho2CAD 3D 重建。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-cad.json"), reverse=True)
    return {"runs": [json.loads(p.read_text(encoding="utf-8")) for p in paths]}


@router.get("/eval/sql-runs", response_model=S.SqlEvalRunsResponse, tags=["eval"])
def sql_eval_runs():
    """工廠庫存 Text-to-SQL 評估（make eval-sql）：執行正確率、可執行率、修正次數。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-sql.json"), reverse=True)
    return {"runs": [json.loads(p.read_text(encoding="utf-8")) for p in paths]}


@router.get("/eval/route-runs", response_model=S.RouteEvalRunsResponse, tags=["eval"])
def route_eval_runs():
    """智慧助理路由評估（make eval-route）：Jev 與本地路由的正確率、修改誤判、延遲、外送量。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-route.json"), reverse=True)[:10]
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for r in runs:  # 清單只回摘要，逐題結果留在檔案
        for e in r.get("engines", []):
            e.pop("rows", None)
    return {"runs": runs}


DEMO_WARNING = "展示模式已開啟：可以不用密碼切換任何身分，不可用於正式環境"


def _index_problems() -> list[str]:
    store = get_store()
    problems = store.check_manifest(store.manifest) if store.manifest else ["索引未載入"]
    if store.manifest and not problems and current_kb_hash() != store.manifest.get("kb_hash"):
        problems.append("kb/ 內容已變更但尚未重建索引（請執行 make index）")
    return problems


@router.get("/health", response_model=S.HealthResponse, tags=["system"])
async def health(response: Response):
    """公開健康檢查（不用憑證）：只回存活與就緒（docs/adr/030）。
    最近的問句、回覆、SQL、路由紀錄、模型端點與內部設定在 /admin/diagnostics（要管理權限）。"""
    # 即時狀態：禁止監控、代理或瀏覽器重用舊結果
    response.headers["Cache-Control"] = "no-store"
    s = get_settings()
    problems = _index_problems()
    db_ok = await run_in_threadpool(get_logs_repo().ping)
    if s.llm_mode == "mock":
        hybrid_ok = True
    else:
        hybrid_ok, _ = await providers.ping_ollama(s.hybrid_base_url)
    return S.HealthResponse(
        status="ok" if db_ok and not problems and hybrid_ok else "degraded",
        ready=db_ok and not problems,
        db=db_ok,
        index_consistent=not problems,
        kb_version=kb_version(),
        demo_controls=s.demo_controls,
        demo_warning=DEMO_WARNING if s.demo_controls else None,
    )


@router.get("/status", response_model=S.StatusResponse, tags=["system"])
async def system_status(request: Request, response: Response):
    """登入後畫面用的系統狀態（要有效 JWT）：服務能不能用、記憶體、展示模式。
    不含問句、回覆、SQL、路由紀錄、模型端點位址與內部設定（docs/adr/030）。"""
    response.headers["Cache-Control"] = "no-store"
    d = await diagnostics_data(with_logs=False)
    s = get_settings()
    ok, _ = jev.status()
    return S.StatusResponse(
        status=d.status,
        outage_simulated=d.outage_simulated,
        demo_controls=identity.demo_allowed(request),
        demo_warning=DEMO_WARNING if s.demo_controls else None,
        eval_controls=identity.eval_allowed(request),
        strategies={
            k: S.StrategyBrief(
                label=v.label,
                model=v.model,
                available=v.available,
                detail="可以使用" if v.available else "目前無法使用",
            )
            for k, v in d.strategies.items()
        },
        memory=d.memory,
        system1=S.System1Brief(
            jev_configured=ok,
            detail="已設定" if ok else "未設定或已停用：第 2、4～6 段改用地端規則",
            model=s.jev_model,
        ),
        jobs=S.JobsBrief(**await run_in_threadpool(jobs_boot.status)),
        chat_defaults=S.ChatDefaultsBrief(
            rearrange=rearrange.enabled(),
            conflict_check=conflict_check.enabled(),
            send_image=chat_service.send_image_enabled(),
        ),
    )


@router.get("/admin/diagnostics", response_model=S.DiagnosticsResponse, tags=["system"])
async def diagnostics(request: Request, response: Response):
    """管理診斷：最近的問答、3D 重建、SQL、路由紀錄、模型端點、索引與內部設定。
    要 access.yaml 的 views.diagnostics（預設只有主管，docs/adr/030）。"""
    identity.require_view(identity.current(request), "diagnostics", "系統診斷")
    response.headers["Cache-Control"] = "no-store"
    return await diagnostics_data()


async def diagnostics_data(with_logs: bool = True) -> S.DiagnosticsResponse:
    """with_logs=False（/status 用）：不查最近的問答、3D、SQL、路由紀錄。"""
    s = get_settings()
    cfg = get_models_config().strategies
    store = get_store()
    problems = _index_problems()

    hybrid_model = s.hybrid_model or cfg["hybrid"].default_model
    fallback_model = s.hybrid_fallback_model or cfg["hybrid_fallback"].default_model
    fallback_url = s.hybrid_fallback_base_url or s.hybrid_base_url
    if s.llm_mode == "mock":
        hybrid_ok, hybrid_detail = True, "LLM_MODE=mock"
        fallback_ok, fallback_detail = True, "LLM_MODE=mock"
        ortho_ok, ortho_detail = True, "LLM_MODE=mock（回傳標準模型程式碼）"
    else:
        hybrid_ok, hybrid_detail = await providers.ping_ollama(s.hybrid_base_url)
        fallback_ok, fallback_detail = await providers.ping_ollama(
            fallback_url, simulate_outage=False
        )
        ortho_ok, ortho_detail = await providers.ping_ollama(
            s.ortho2cad_base_url, simulate_outage=False
        )
        if not ortho_ok:
            ortho_detail += "；請執行 make ortho2cad"
    cloud_ok, cloud_detail = providers.cloud_status()
    strategies = {
        "hybrid": S.StrategyStatus(
            label=cfg["hybrid"].label,
            model=hybrid_model,
            available=hybrid_ok,
            detail=f"{s.hybrid_base_url}（{hybrid_detail}）",
        ),
        "hybrid_fallback": S.StrategyStatus(
            label=cfg["hybrid_fallback"].label,
            model=fallback_model,
            available=fallback_ok,
            detail=f"{fallback_url}（{fallback_detail}）",
        ),
        **{
            k: S.StrategyStatus(
                label=cfg[k].label,
                model=s.api_model or cfg[k].default_model,
                available=cloud_ok,
                detail=cloud_detail,
            )
            for k in ("api_nokb", "api_kb")
        },
        "ortho2cad": S.StrategyStatus(
            label=cfg["ortho2cad"].label,
            model=s.ortho2cad_model or cfg["ortho2cad"].default_model,
            available=ortho_ok,
            detail=f"{s.ortho2cad_base_url}（{ortho_detail}）",
        ),
        "lora": S.StrategyStatus(
            label=cfg["lora"].label,
            model=s.lora_model or cfg["lora"].default_model,
            available=s.lora_enabled,
            detail="已啟用" if s.lora_enabled else "選做：插槽已保留，未啟用",
        ),
    }
    logs = get_logs_repo()
    # PostgreSQL 容器停了也要回得出狀態頁：ping 放到執行緒（最多等 5 秒，不卡住其他請求），
    # 失敗就不查最近紀錄
    db_ok = await run_in_threadpool(logs.ping)
    inv = get_inventory_repo()
    inv_ok = inv.ping()
    scheduler = await schedule_service.engine_status()
    memory = await asyncio.to_thread(memory_guard.guard.status)
    return S.DiagnosticsResponse(
        status="ok" if db_ok and not problems and hybrid_ok else "degraded",
        db=db_ok,
        index_consistent=not problems,
        index_problems=problems,
        kb_version=kb_version(),
        manifest=store.manifest,
        strategies=strategies,
        llm_mode=s.llm_mode,
        embed_mode=s.embed_mode,
        allow_cloud=s.allow_cloud,
        outage_simulated=providers.OUTAGE["enabled"],
        demo_controls=s.demo_controls,
        eval_controls=s.eval_controls,
        recent_chats=logs.recent_chats(10) if db_ok and with_logs else [],
        recent_cad=logs.recent_cad(10) if db_ok and with_logs else [],
        inventory={
            "ok": inv_ok,
            "as_of": inv.as_of,
            "tables": inv.manifest.get("tables", {}),
            "problems": inv.problems,
        },
        recent_sql=logs.recent_sql(10) if db_ok and with_logs else [],
        scheduler=scheduler,
        memory=memory,
        system1=_system1_status(),
        recent_routes=logs.recent_routes(10) if db_ok and with_logs else [],
    )


def _system1_status() -> S.System1Status:
    s = get_settings()
    cfg = get_agent_config()
    ok, detail = jev.status()
    return S.System1Status(
        jev_configured=ok,
        detail=detail,
        model=s.jev_model,
        timeout_s=s.jev_timeout_s,
        thresholds=cfg["thresholds"],
        clarify_margin=cfg["clarify_margin"],
    )


@router.post("/admin/outage", response_model=S.OkResponse, tags=["system"])
def simulate_outage(body: S.OutageRequest, request: Request):
    """展示用：模擬主推論伺服器斷線，本地備援模型不受影響（只在展示模式、本機，docs/adr/030）。"""
    identity.require_demo(request, "模擬斷線")
    providers.OUTAGE["enabled"] = body.enabled
    return S.OkResponse()


@router.get("/security/logs", response_model=S.SecurityLogsResponse, tags=["agent"])
def security_logs(request: Request, limit: int = Query(default=20, ge=1, le=200)):
    """七段權限控管的拒絕並記錄（docs/adr/015）：第 1 段（憑證無效、角色不符）、
    第 2 段 Jev Choice 擋下的請求、第 4 段剔除的洩密段落、第 7 段輸出檢查擋下的回覆。
    只存事件類型、文件／段落 ID 與雜湊；要 access.yaml 的 views.security_logs（docs/adr/030）。"""
    account = identity.current(request)
    identity.require_view(account, "security_logs", "拒絕並記錄")
    repo = get_logs_repo()
    tz = timezone(timedelta(hours=8))
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = repo.count_security(midnight.astimezone(UTC).isoformat())
    return {
        "items": repo.recent_security(limit),
        "today": {
            "rbac": counts.get(1, 0),
            "guard": counts.get(2, 0),
            "post": counts.get(4, 0),
            "output": counts.get(7, 0),
        },
    }


@router.get("/audit", response_model=S.AuditResponse, tags=["agent"])
def audit_log(request: Request, limit: int = Query(default=30, ge=1, le=200)):
    """稽核紀錄（寫入、拒絕、送核准、核准、退回、失效）與最近的異動單。
    要 access.yaml 的 views.audit（docs/adr/030）。"""
    account = identity.current(request)
    identity.require_view(account, "audit", "稽核紀錄")
    # 3D 重建結果的作廢、採用：稽核資料庫故障時先存在快取的待送清單，讀之前先補送（docs/adr/042）
    with suppress(Exception):
        cad_cache.flush_audit()
    prod = get_production_repo()
    return {"items": prod.audit(limit), "changes": prod.changes(limit=10)}
