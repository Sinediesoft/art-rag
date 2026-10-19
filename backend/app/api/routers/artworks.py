"""畫作圖紙：公開畫作的辨識、搜尋、清單、色彩與藝術家區域註記（問答在 chat.py，七段共用）。"""

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

from app.api import schemas as S
from app.core.config import (
    REPO_ROOT,
    get_settings,
)
from app.core.errors import AppError
from app.repositories.index_store import get_store
from app.services import (
    color_service,
    identity,
    region_service,
    search_service,
    uploads,
)

router = APIRouter()


@router.post("/search/image", response_model=S.ImageSearchResponse, tags=["search"])
def search_image(body: S.ImageSearchRequest, request: Request):
    account = identity.current(request)
    uploads.authorize(body.image_id, account)  # 不是自己的照片 → 404，不讀圖、不辨識
    return search_service.identify(body.image_id, body.top_k, viewer=account)


@router.get("/search/text", response_model=S.TextSearchResponse, tags=["search"])
def search_text(q: str = Query(min_length=1, max_length=200), top_k: int | None = None):
    return search_service.search_text(q, top_k)


@router.get("/artworks", response_model=S.ArtworkListResponse, tags=["artworks"])
def list_artworks():
    store = get_store()
    return {
        "kb_version": store.manifest.get("kb_version", ""),
        "items": [search_service.artwork_summary(a) for a in store.artworks],
    }


@router.get("/artworks/{artwork_id}", response_model=S.ArtworkDetail, tags=["artworks"])
def get_artwork(artwork_id: str):
    a = get_store().get_artwork(artwork_id)
    if not a:
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    summary = search_service.artwork_summary(a)
    return {**a, "image_url": summary["image_url"], "thumb_url": summary["thumb_url"]}


@router.get("/artworks/{artwork_id}/image", response_class=FileResponse, tags=["artworks"])
def get_artwork_image(artwork_id: str, size: str = "full"):
    a = get_store().get_artwork(artwork_id)
    if not a:
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    path: Path = (
        get_settings().index_dir / "thumbs" / f"{artwork_id}.jpg"
        if size == "thumb"
        else REPO_ROOT / a["image"]["path"]
    )
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/artworks/{artwork_id}/colors", response_model=S.ColorAnalysis, tags=["artworks"])
def get_artwork_colors(artwork_id: str):
    return color_service.artwork_colors(artwork_id)


@router.get("/artworks/{artwork_id}/colormap.png", response_class=FileResponse, tags=["artworks"])
def get_artwork_colormap(artwork_id: str):
    return FileResponse(
        color_service.artwork_colormap_path(artwork_id),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# -------------------------------------------------------- 畫面區域的草稿與收錄（docs/adr/029）
@router.post("/artworks/{artwork_id}/region-drafts", response_model=S.RegionDraft, tags=["regions"])
def create_region_draft(artwork_id: str, body: S.RegionDraftRequest, request: Request):
    """藝術家在畫上圈一塊、寫解說（kb_annotate，只能標分給自己的畫）。送出就是草稿，等主管收錄；
    解說先過輸入防護的地端規則，像在對 AI 下指令就回 REGION_REJECTED。"""
    return region_service.create_draft(
        artwork_id, body.model_dump(), identity.current(request), request.state.request_id
    )


@router.get("/region-drafts", response_model=S.RegionDraftsResponse, tags=["regions"])
def list_region_drafts(request: Request):
    """待收錄（能收錄的身分才看得到）與自己送出的草稿。"""
    return region_service.list_drafts(identity.current(request))


@router.post("/region-drafts/{draft_id}/commit", response_model=S.RegionDraft, tags=["regions"])
def commit_region_draft(draft_id: str, request: Request):
    """收錄（kb_intake，只有主管）：寫進 kb/artworks/<id>.json、遞增 kb/VERSION、背景重建索引。

    回傳時狀態是 indexing；重建完成後變成 done（或 failed，已還原）。"""
    return region_service.commit_draft(
        draft_id, identity.current(request), request.state.request_id
    )


@router.post("/region-drafts/{draft_id}/return", response_model=S.RegionDraft, tags=["regions"])
def return_region_draft(draft_id: str, body: S.RegionDraftReturn, request: Request):
    """主管退回（附原因），藝術家在自己的草稿清單看得到。"""
    return region_service.return_draft(
        draft_id, body.reason, identity.current(request), request.state.request_id
    )


@router.delete("/region-drafts/{draft_id}", response_model=S.OkResponse, tags=["regions"])
def withdraw_region_draft(draft_id: str, request: Request):
    """送出的人撤回還沒收錄的草稿。"""
    region_service.withdraw_draft(draft_id, identity.current(request))
    return S.OkResponse()
