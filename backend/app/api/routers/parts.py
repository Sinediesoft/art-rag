"""工廠圖紙：清單、單筆、原圖／模型、候選比對（以圖／以文）。"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from app.agent import guard
from app.api import schemas as S
from app.api.deps import scope_drawing_result, visible_part
from app.core.config import (
    REPO_ROOT,
)
from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.services import (
    identity,
    search_service,
    uploads,
)

router = APIRouter()


@router.get("/parts", response_model=S.PartListResponse, tags=["parts"])
def list_parts(request: Request):
    """只列目前身分看得到的圖紙（機密等級＋部門）：業務看不到機密圖紙；
    一般員工不能使用工廠圖紙 → 403。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    store = get_store()
    items = [p for p in store.parts if identity.can_view_part(account, p)]
    return {
        "kb_version": store.manifest.get("kb_version", ""),
        "items": [search_service.part_summary(p) for p in items],
        "hidden": len(store.parts) - len(items),
    }


@router.get("/parts/{part_id}", response_model=S.PartDetail, tags=["parts"])
def get_part(part_id: str, request: Request):
    p = visible_part(part_id, request)
    summary = search_service.part_summary(p)
    return {
        **p,
        "drawing_url": summary["drawing_url"],
        "thumb_url": summary["thumb_url"],
        "model_url": summary["model_url"],
        "step_url": f"/api/v1/parts/{part_id}/model.step" if "cad" in p else None,
    }


@router.get("/parts/{part_id}/drawing", response_class=FileResponse, tags=["parts"])
def get_part_drawing(part_id: str, request: Request, size: str = "full"):
    p = visible_part(part_id, request)
    store = get_store()
    path = (
        store.parts_dir / "thumbs" / f"{part_id}.jpg"
        if size == "thumb"
        else REPO_ROOT / p["drawing"]
    )
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/parts/{part_id}/model.{ext}", response_class=FileResponse, tags=["parts"])
def get_part_model(part_id: str, ext: str, request: Request):
    """知識庫零件的標準 3D 模型（由 kb/cad/<id>.py 產生）：stl 給前端 3D 檢視、step 給 CAD 軟體。"""
    p = visible_part(part_id, request)
    if ext not in ("stl", "step"):
        raise AppError("VALIDATION_ERROR", "只提供 stl 或 step", 422)
    path = get_store().parts_dir / "gt" / f"{part_id}.{ext}"
    if "cad" not in p or not path.is_file():
        raise AppError("PART_MODEL_NOT_FOUND", f"圖紙 {part_id} 沒有標準 3D 模型（照片建檔）", 404)
    media = "model/stl" if ext == "stl" else "application/step"
    return FileResponse(path, media_type=media, filename=f"{part_id}.{ext}")


@router.post("/search/drawing", response_model=S.DrawingSearchResponse, tags=["search"])
def search_drawing(body: S.DrawingSearchRequest, request: Request):
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    uploads.authorize(body.image_id, account)  # 不是自己的照片 → 404，不讀圖、不辨識
    # 先做文件層授權：只在看得到的圖紙裡辨識（docs/adr/030）；事後過濾留著當第二道
    visible = identity.visible_part_ids(account, get_store().parts)
    return scope_drawing_result(
        search_service.identify_drawing(
            body.image_id, body.top_k, part_ids=visible, viewer=account
        ),
        account,
    )


@router.get("/search/parts", response_model=S.PartTextSearchResponse, tags=["search"])
def search_parts(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    top_k: int | None = None,
):
    """以文字找圖紙：只檢索目前身分看得到的圖紙段落（Metadata Filter：機密等級＋部門）。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    scope = guard.MetaFilter.of(account, "mfg")
    return search_service.search_parts_text(
        q, top_k, identity.visible_part_ids(account, get_store().parts), scope.text
    )
