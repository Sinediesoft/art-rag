"""共用：上傳照片與衍生資料（色彩、畫作卡、對位）、不指定領域的以圖搜圖。照片只有擁有者與其授權的帳號能用。"""

import uuid

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from app.api import schemas as S
from app.api.deps import scope_drawing_result, visible_part
from app.core.config import (
    get_settings,
)
from app.core.errors import AppError
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.repositories.index_store import get_store
from app.services import (
    color_service,
    compare_service,
    identity,
    search_service,
    style_service,
    uploads,
)

router = APIRouter()

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


@router.post("/images", response_model=S.ImageUploadResponse, tags=["images"])
async def upload_image(request: Request, file: UploadFile = File(...)):
    """上傳照片：記下擁有者（目前帳號）。之後原圖、色彩、對位、辨識、問答、建檔、批次與 3D 重建
    都只接受擁有者與擁有者授權（PUT /images/{id}/acl）的帳號；知道 image_id 不夠。"""
    owner = identity.current(request)
    s = get_settings()
    if file.content_type not in ALLOWED_TYPES:
        raise AppError("IMAGE_TYPE_NOT_ALLOWED", "只收 JPEG、PNG、WebP", 415)
    raw = await file.read()
    if len(raw) > s.upload_max_mb * 1024 * 1024:
        raise AppError("IMAGE_TOO_LARGE", f"圖片超過 {s.upload_max_mb} MB", 413)
    try:
        img = load_image(raw)  # EXIF 轉正、縮圖；重新編碼後不留 EXIF／GPS
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        # 宣稱是圖片但讀不出來：回 415，不讓 PIL 的例外（含記憶體位址）變成 500 回給使用者
        raise AppError("IMAGE_TYPE_NOT_ALLOWED", "讀不出這張圖片，只收 JPEG、PNG、WebP", 415) from e
    image_id = "img_" + uuid.uuid4().hex[:16]
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    (s.uploads_dir / f"{image_id}.jpg").write_bytes(to_jpeg_bytes(img))
    uploads.register(image_id, f"{image_id}.jpg", owner)
    return S.ImageUploadResponse(image_id=image_id, width=img.width, height=img.height)


@router.get("/images/{image_id}", response_class=FileResponse, tags=["images"])
def get_image(image_id: str, request: Request):
    """擁有者與授權帳號才讀得到；不存在和沒有權限同一個 404（不透露別人的照片存不存在）。"""
    rec = uploads.record_of(image_id, identity.current(request))
    return FileResponse(
        get_settings().uploads_dir / rec["path"],
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.put("/images/{image_id}/acl", response_model=S.ImageAcl, tags=["images"])
def set_image_acl(image_id: str, body: S.ImageAclUpdate, request: Request):
    """擁有者明確授權其他帳號使用這張照片（整份取代；清單空白＝只有自己）。非擁有者 404。"""
    owner = identity.current(request)
    acl = uploads.set_acl(image_id, owner, body.accounts, set(identity.accounts()))
    return {"image_id": image_id, "owner_id": owner.id, "accounts": acl}


@router.get("/images/{image_id}/colors", response_model=S.ColorAnalysis, tags=["images"])
def get_photo_colors(image_id: str, request: Request):
    return color_service.photo_colors(image_id, identity.current(request))


@router.get("/images/{image_id}/colormap.png", response_class=Response, tags=["images"])
def get_photo_colormap(image_id: str, request: Request):
    png = color_service.photo_colormap(image_id, identity.current(request))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.get("/images/{image_id}/style", response_model=S.StyleGuess, tags=["images"])
def get_photo_style(image_id: str, request: Request):
    """知識庫沒有這幅畫時的畫作卡：Chinese-CLIP 零樣本推測風格大類、題材、媒材（docs/adr/018）。
    只是推測、沒有出處，不寫進知識庫、不放進問答的 prompt。"""
    return style_service.photo_style(image_id, identity.current(request))


_TARGET = Query(
    pattern=compare_service.TARGET_PATTERN,
    description="比對對象：artwork:<畫作 id>、part:<圖紙 id>，或 image:<另一張上傳照片>（照片 A）",
    examples=["part:mfg-001"],
)


def _align_scope(target: str, request: Request) -> None:
    """和圖紙比對也是讀圖紙（docs/adr/014 的資料範圍）：回應與疊圖都含圖紙內容，
    看不到的圖紙 → 403。"""
    kind, _, tid = target.partition(":")
    if kind == "part":
        visible_part(tid, request)


@router.get("/images/{image_id}/align", response_model=S.ImageAlignment, tags=["images"])
def get_photo_alignment(image_id: str, request: Request, target: str = _TARGET):
    """影像對位與比對（docs/adr/012）：照片拍到參考圖的哪一塊，再找出不一樣的地方——
    畫作（artwork:、另一張照片 image:）比形狀與顏色，圖紙（part:）比三視圖的線條。
    圖紙照資料範圍：一般員工不能用、業務看不到機密圖紙（403 DATA_SCOPE_DENIED）。"""
    _align_scope(target, request)
    return compare_service.align_photo(image_id, target, identity.current(request))


@router.get("/images/{image_id}/align.png", response_class=Response, tags=["images"])
def get_photo_alignment_overlay(image_id: str, request: Request, target: str = _TARGET):
    _align_scope(target, request)
    png = compare_service.align_overlay(image_id, target, identity.current(request))
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@router.post("/search/any", response_model=S.AnySearchResponse, tags=["search"])
def search_any(body: S.ImageSearchRequest, request: Request):
    """不指定領域的以圖搜圖：先判斷是畫作還是工廠圖紙（領域路由），再做該領域的辨識。
    判成工廠圖紙時要看目前身分的資料範圍（一般員工不能使用工廠圖紙 → 403）。"""
    account = identity.current(request)
    uploads.authorize(body.image_id, account)  # 照片不是自己的（也沒被授權）→ 404，不讀圖、不辨識
    # 判成圖紙時只在看得到的圖紙裡辨識（一般員工是空集合：不讀任何圖紙），之後照樣檢查領域
    visible = identity.visible_part_ids(account, get_store().parts)
    result = search_service.identify_any(
        body.image_id, body.top_k, part_ids=visible, viewer=account
    )
    if result["drawing_result"] is not None:
        identity.require_domain(account, "mfg")
        result["drawing_result"] = scope_drawing_result(result["drawing_result"], account)
    return result
