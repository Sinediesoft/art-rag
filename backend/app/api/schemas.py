"""API 請求／回應模型：FastAPI 依此產生 shared/openapi.json，前端型別再由它自動產生。"""

from typing import Literal

from pydantic import BaseModel, Field

# api_nokb／api_kb 是雲端對照組（A1 無檢索、A2 有檢索），ALLOW_CLOUD=false 時停用
Strategy = Literal["hybrid", "api_nokb", "api_kb", "lora", "mock"]


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ImageUploadResponse(BaseModel):
    image_id: str
    width: int
    height: int


class ArtworkSummary(BaseModel):
    id: str
    title_zh: str
    title_en: str | None = None
    artist_zh: str
    artist_en: str | None = None
    date_text: str
    collection: str
    image_url: str
    thumb_url: str
    style_tags: list[str] = []


class ImageSearchRequest(BaseModel):
    image_id: str
    top_k: int | None = Field(default=None, ge=1, le=20)


class ImageSearchHit(BaseModel):
    artwork: ArtworkSummary
    score: float = Field(description="Chinese-CLIP 餘弦相似度")
    inliers: int | None = Field(description="ORB 幾何驗證的 inlier 數；未驗證為 null")
    verified: bool


class ImageSearchResponse(BaseModel):
    query_image_id: str
    threshold: float
    min_inliers: int
    matched: bool
    best_artwork_id: str | None
    latency_ms: int
    results: list[ImageSearchHit]


class TextSearchHit(BaseModel):
    artwork: ArtworkSummary
    score: float
    image_score: float
    text_score: float


class TextSearchResponse(BaseModel):
    query: str
    latency_ms: int
    results: list[TextSearchHit]


class Description(BaseModel):
    lang: str
    topic: str | None = None
    text: str
    source_url: str
    license: str
    attribution: str | None = None


class ArtworkImage(BaseModel):
    path: str
    license: str
    attribution: str | None = None
    source_url: str | None = None


class ArtworkDetail(BaseModel):
    id: str
    source_id: str | None = None
    title: dict[str, str]
    artist: dict[str, str]
    date_text: str
    medium: str | None = None
    dimensions: str | None = None
    collection: str
    image: ArtworkImage
    source_url: str
    descriptions: list[Description]
    style_tags: list[str] = []
    image_url: str
    thumb_url: str


class ArtworkListResponse(BaseModel):
    kb_version: str
    items: list[ArtworkSummary]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    artwork_id: str | None = None
    part_id: str | None = Field(default=None, description="工廠圖紙問答；與 artwork_id 擇一")
    image_id: str | None = None
    strategy: Strategy = "hybrid"
    use_retrieval: bool = True
    allow_fallback: bool = True


class FeedbackRequest(BaseModel):
    request_id: str
    rating: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=500)


class OkResponse(BaseModel):
    ok: bool = True


class StrategyStatus(BaseModel):
    label: str
    model: str
    available: bool
    detail: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: bool
    index_consistent: bool
    index_problems: list[str]
    kb_version: str
    manifest: dict
    strategies: dict[str, StrategyStatus]
    llm_mode: str
    embed_mode: str
    allow_cloud: bool
    outage_simulated: bool
    demo_controls: bool
    recent_chats: list[dict]
    recent_cad: list[dict] = []


class EvalRun(BaseModel):
    run_id: str
    created_at: str
    kb_version: str
    prompt_version: str
    summary: dict
    by_strategy: dict


class EvalRunsResponse(BaseModel):
    runs: list[EvalRun]


class OutageRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------- 工廠機械加工圖
class PartGeometry(BaseModel):
    width: float = Field(description="X 方向外形尺寸（mm）")
    depth: float = Field(description="Y 方向外形尺寸（mm）")
    height: float = Field(description="Z 方向外形尺寸（mm）")
    volume_mm3: float
    weight_kg: float = Field(description="標準模型體積 × 材料密度")
    faces: int


class PartSummary(BaseModel):
    id: str
    part_no: str
    drawing_no: str
    revision: str
    name_zh: str
    name_en: str | None = None
    category: str
    material: str
    confidentiality: Literal["公開", "內部", "機密"]
    geometry: PartGeometry
    drawing_url: str
    thumb_url: str
    model_url: str = Field(description="標準 3D 模型（STL）")
    tags: list[str] = []


class PartDescription(BaseModel):
    topic: str
    text: str
    source: str


class PartDetail(BaseModel):
    id: str
    part_no: str
    drawing_no: str
    revision: str
    name: dict[str, str]
    category: str
    material: str
    density_g_cm3: float
    surface: str | None = None
    company: str
    owner: str
    confidentiality: Literal["公開", "內部", "機密"]
    geometry: PartGeometry
    descriptions: list[PartDescription]
    tags: list[str] = []
    drawing_url: str
    thumb_url: str
    model_url: str
    step_url: str


class PartListResponse(BaseModel):
    kb_version: str
    items: list[PartSummary]


class DrawingSearchRequest(BaseModel):
    image_id: str
    top_k: int | None = Field(default=None, ge=1, le=20)


class DrawingSearchHit(BaseModel):
    part: PartSummary
    score: float = Field(description="Chinese-CLIP 餘弦相似度")
    inliers: int | None = Field(description="ORB 幾何驗證的 inlier 數；未驗證為 null")
    overlap: float | None = Field(description="拉正後的線條重合度（0–1）；未驗證為 null")
    verified: bool


class DrawingSearchResponse(BaseModel):
    query_image_id: str
    threshold: float
    min_inliers: int
    min_overlap: float
    matched: bool
    best_part_id: str | None
    latency_ms: int
    results: list[DrawingSearchHit]


class PartTextSearchHit(BaseModel):
    part: PartSummary
    score: float
    topic: str
    snippet: str


class PartTextSearchResponse(BaseModel):
    query: str
    latency_ms: int
    results: list[PartTextSearchHit]


class ReconstructRequest(BaseModel):
    part_id: str | None = Field(default=None, description="知識庫圖紙")
    image_id: str | None = Field(
        default=None, description="上傳的圖紙照片；與 part_id 同時給代表照片已辨識為該圖紙"
    )
    strategy: Literal["ortho2cad", "hybrid"] = Field(
        default="ortho2cad", description="ortho2cad＝主模型；hybrid＝未微調的 Qwen3-VL（對照組）"
    )


class CadJobSummary(BaseModel):
    meta: dict
    result: dict
    done: dict


class CadJobRow(BaseModel):
    job_id: str
    created_at: str
    strategy: str
    model: str
    ok: bool
    iou: float | None = None
    iou_bbox: float | None = None
    total_ms: int | None = None


class CadJobListResponse(BaseModel):
    items: list[CadJobRow]


class CadEvalRunsResponse(BaseModel):
    runs: list[dict]
