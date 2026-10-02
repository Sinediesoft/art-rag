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


# ---------------------------------------------------------------- 色彩分析（docs/adr/010）
class PaletteColor(BaseModel):
    hex: str = Field(description="#RRGGBB")
    rgb: list[int]
    lab: list[float]
    share: float = Field(description="占畫面比例 0–1")
    name: str = Field(description="最接近的基本色名（CIEDE2000）")
    temperature: Literal["warm", "cool", "neutral"]
    tone: Literal["dark", "mid", "light"]


class TemperatureShare(BaseModel):
    warm: float
    cool: float
    neutral: float


class LightnessStats(BaseModel):
    dark: float
    mid: float
    light: float
    mean: float
    p5: float
    p95: float
    histogram: list[float] = Field(description="L* 0–100 分 10 格的比例")


class ChromaStats(BaseModel):
    low: float
    mid: float
    high: float
    median: float
    histogram: list[float] = Field(description="C* 0–100 分 10 格的比例（≥100 算在最後一格）")


class ColorAnalysis(BaseModel):
    source: Literal["original", "photo"] = Field(
        description="original＝知識庫原圖；photo＝上傳的照片"
    )
    method: str
    palette: list[PaletteColor]
    temperature: TemperatureShare
    lightness: LightnessStats
    chroma: ChromaStats
    summary: str
    notes: list[str]
    map_url: str = Field(description="色塊分布圖 PNG（每個像素塗成所屬主色）")
    latency_ms: int = Field(description="計算耗時；知識庫畫作為建索引時算好的，回 0")


# ---------------------------------------------------------------- 影像對位與比對（docs/adr/012）
class AlignTarget(BaseModel):
    kind: Literal["artwork", "part", "image"] = Field(
        description="artwork：知識庫畫作原圖；part：知識庫圖紙；image：另一張上傳照片（兩張照片互比）"
    )
    id: str


class AlignLocation(BaseModel):
    polygon: list[list[float]] = Field(
        description="照片四個角（左上、右上、右下、左下）在參考圖上的位置，0–1；"
        "照片拍到參考圖外面時會超出 0–1"
    )
    coverage: float = Field(description="照片拍到參考圖面積的比例 0–1")
    center: list[float] = Field(description="拍到的範圍的中心 [x, y]，0–1")


class AlignRegion(BaseModel):
    bbox: list[float] = Field(description="[x0, y0, x1, y1]，0–1，參考圖座標")
    kind: Literal["missing", "extra", "shape", "color", "both"] = Field(
        description="ink（圖紙）：missing＝知識庫圖紙有、照片沒有，extra＝照片有、知識庫圖紙沒有；"
        "tone（畫作：照片 vs 原圖、兩張照片）：shape＝形狀不同，color＝顏色不同；both＝兩種都有"
    )
    area_ratio: float = Field(
        description="這處差異的大小：ink 是占知識庫圖紙線條像素的比例，tone 是占比對範圍的比例"
    )


class AlignDiff(BaseModel):
    method: Literal["ink", "tone"] = Field(
        description="ink：拉正後比對三視圖的線條（圖紙）；"
        "tone：比形狀與顏色（畫作：照片 vs 知識庫原圖，或兩張照片）"
    )
    status: Literal["same", "changed", "global_change"] = Field(
        description="same：沒有差異；changed：列出差異；global_change：差異遍布整張，不列區塊"
        "（圖紙多半是改了外形尺寸；畫作多半是光線差太多、大片反光、照片太模糊，或拍的不是同一處）"
    )
    regions: list[AlignRegion] = Field(description="依差異大小排序；global_change 時是空的")
    changed_ratio: float = Field(
        description="所有差異的大小：ink 占知識庫圖紙線條像素、tone 占比對範圍的比例"
    )


class ImageAlignment(BaseModel):
    target: AlignTarget
    inliers: int = Field(description="照片與參考圖對上的特徵點數（RANSAC inlier）")
    location: AlignLocation
    diff: AlignDiff | None = Field(
        description="知識庫畫作原圖、另一張照片為形狀與顏色差異（tone）；圖紙為線條差異（ink）；"
        "image_compare 設成 diff: none 的領域為 null（只標位置）"
    )
    reference_url: str = Field(description="參考圖：知識庫畫作原圖或圖紙")
    overlay_url: str = Field(
        description="疊圖 PNG：參考圖上差異塗色並編號（畫作沒拍到的地方調暗）；"
        "只標位置時是框出拍到的範圍"
    )
    notes: list[str]
    latency_ms: int = Field(description="對位與比對的計算時間（不含讀檔）")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    artwork_id: str | None = None
    part_id: str | None = Field(default=None, description="工廠圖紙問答；與 artwork_id 擇一")
    image_id: str | None = None
    strategy: Strategy = "hybrid"
    use_retrieval: bool = True
    allow_fallback: bool = True
    rearrange: bool | None = Field(
        default=None,
        description="檢索段落篩選（MIRA 的 Rearrange）；null＝依伺服器設定（預設關）",
    )


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
    inventory: dict = Field(default={}, description="庫存資料庫：資料日期、各表筆數、資料問題")
    recent_sql: list[dict] = []
    scheduler: "SchedulerEngine | None" = None
    memory: "MemoryStatus | None" = None
    system1: "System1Status | None" = None
    recent_routes: list[dict] = []


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
    """有標準模型時由模型計算；照片建檔的零件（docs/adr/013）只有圖上標註的外形，其餘為 null。"""

    width: float = Field(description="X 方向外形尺寸（mm）")
    depth: float = Field(description="Y 方向外形尺寸（mm）")
    height: float = Field(description="Z 方向外形尺寸（mm）")
    volume_mm3: float | None = Field(description="標準模型體積；沒有標準模型為 null")
    weight_kg: float | None = Field(description="標準模型體積 × 材料密度；沒有標準模型為 null")
    faces: int | None


class PartIntakeInfo(BaseModel):
    """照片建檔紀錄（part.schema.json 的 intake）。"""

    method: Literal["photo"]
    date: str
    draft_id: str | None = None
    model: str | None = Field(default=None, description="讀標題欄的模型")
    fields_from_model: list[str] = Field(default=[], description="由模型讀取、人沒有改過的欄位")
    confirmed_by: str = Field(description="按「收錄」的展示帳號")


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
    model_url: str | None = Field(description="標準 3D 模型（STL）；照片建檔的零件沒有，為 null")
    intake: bool = Field(default=False, description="照片建檔的零件（docs/adr/013）")
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
    model_url: str | None
    step_url: str | None
    intake: PartIntakeInfo | None = None


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


class RouteInfo(BaseModel):
    """領域路由（MMed-RAG 的領域辨識）：照片是畫作還是工廠圖紙"""

    domain: Literal["art", "mfg"] = Field(description="art：畫作；mfg：工廠圖紙")
    margin: float = Field(description="與圖紙原型的相似度 − 與畫作原型的相似度；> 0 偏向圖紙")
    art_score: float | None = Field(description="與畫作原型（知識庫畫作 CLIP 向量的平均）的相似度")
    mfg_score: float | None = Field(description="與圖紙原型的相似度")
    min_margin: float = Field(description="|margin| 小於此值視為不確定")
    uncertain: bool = Field(description="不確定時一律當圖紙（機密側）")


class AnySearchResponse(BaseModel):
    query_image_id: str
    route: RouteInfo
    artwork_result: ImageSearchResponse | None = Field(description="判定為畫作時的辨識結果")
    drawing_result: DrawingSearchResponse | None = Field(description="判定為圖紙時的辨識結果")
    latency_ms: int


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


class SqlEvalRunsResponse(BaseModel):
    runs: list[dict]


# ---------------------------------------------------------------- 工廠庫存（Text-to-SQL）
class InventoryAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    strategy: Strategy = Field(
        default="hybrid",
        description="hybrid＝本地 Qwen3-VL；雲端策略一律回 CLOUD_CONFIDENTIAL_FORBIDDEN",
    )
    allow_fallback: bool = True


class InventoryColumn(BaseModel):
    name: str
    type: str
    description: str


class InventoryTable(BaseModel):
    name: str
    description: str
    kind: Literal["table", "view"]
    rows: int | None = Field(description="資料筆數；檢視表為 null")
    columns: list[InventoryColumn]


class InventorySchemaResponse(BaseModel):
    as_of: str = Field(description="資料日期（Text-to-SQL 把它當成「今天」）")
    company: str
    tables: list[InventoryTable]
    prompt_version: str
    examples: list[str] = Field(description="few-shot 範例的問題（prompt 內的範例）")


class InventoryOverviewRow(BaseModel):
    part_id: str
    part_no: str
    name: str
    unit: str | None = None
    std_cost_twd: float | None = None
    available: int = Field(description="可用")
    reserved: int = Field(description="保留給訂單")
    inspecting: int = Field(description="待檢")
    defective: int = Field(description="不良")
    on_hand: int = Field(description="合計（所有狀態）")
    safety_stock: int | None = None
    open_demand: int = Field(description="未出貨訂單的需求量")
    in_production: int = Field(description="未完工工單的剩餘數量")


class InventoryOverviewResponse(BaseModel):
    as_of: str
    company: str
    items: list[InventoryOverviewRow]


class StockLocation(BaseModel):
    warehouse_id: str
    warehouse_name: str
    bin: str
    lot_no: str
    status: str
    qty: int
    received_on: str
    note: str | None = None


class OpenWorkOrder(BaseModel):
    wo_no: str
    qty_planned: int
    qty_done: int
    status: str
    line: str
    start_on: str
    due_on: str
    note: str | None = None


class OpenSalesOrder(BaseModel):
    so_no: str
    line_no: int
    customer: str
    qty: int
    qty_shipped: int
    due_on: str
    status: str


class StockMove(BaseModel):
    moved_on: str
    warehouse_id: str
    move_type: str
    qty: int
    ref_no: str | None = None
    note: str | None = None


class PartInventory(InventoryOverviewRow):
    reorder_qty: int | None = None
    lead_time_days: int | None = None
    make_or_buy: str | None = None
    as_of: str
    locations: list[StockLocation]
    work_orders: list[OpenWorkOrder] = Field(description="未完工的工單")
    sales_orders: list[OpenSalesOrder] = Field(description="未出完貨的訂單")
    recent_moves: list[StockMove] = Field(description="最近 8 筆異動")


# ---------------------------------------------------------------- 記憶體管理
class MemoryModel(BaseModel):
    key: str = Field(description="clip／bge／qwen／ortho2cad／timefold")
    label: str
    where: str = Field(description="後端行程／Ollama／llama-server／JVM")
    approx_mb: int
    loaded: bool | None = Field(description="是否載入中；null＝連不上或不在本機")
    in_use: bool = Field(description="有請求正在使用（不會被釋放）")
    needed_by_current_flow: bool


class MemoryReleased(BaseModel):
    key: str
    label: str
    detail: str
    approx_mb: int | None = None


class MemoryEvent(BaseModel):
    at: str
    trigger: str = Field(description="進入「…」流程／背景監控／手動")
    flow: str | None
    flow_label: str
    threshold: float
    percent_before: float
    percent_after: float
    released: list[MemoryReleased]
    failed: list[MemoryReleased]
    kept: list[str] = Field(description="目前流程或其他請求正在用、所以保留的模型")


class MemoryStatus(BaseModel):
    enabled: bool
    percent: float = Field(description="系統記憶體使用率（%）")
    threshold: float
    total_mb: int
    available_mb: int
    current_flow: str | None
    current_flow_label: str | None
    flow_at: str | None
    models: list[MemoryModel]
    events: list[MemoryEvent]


class MemoryReleaseResponse(BaseModel):
    event: MemoryEvent | None
    status: MemoryStatus


# ---------------------------------------------------------------- 生產排程（Timefold）
class SchedulerEngine(BaseModel):
    available: bool
    engine: Literal["timefold", "greedy"]
    version: str | None = None
    java: str | None = None
    memory: dict | None = Field(default=None, description="JVM heap（MB）")
    active_jobs: int | None = None
    detail: str


class RoutingOp(BaseModel):
    op_seq: int
    name: str
    kind: Literal["自製", "委外"]
    machine_type: str | None = None
    setup_min: float | None = None
    run_min_per_pc: float | None = None
    outsource_days: int | None = None
    machines: list[str] = Field(default=[], description="這個機型的機台")


class ScheduleWorkOrder(BaseModel):
    wo_no: str
    part_id: str
    part_no: str
    part_name: str
    qty: int = Field(description="要排程的數量（生產中的工單為剩餘數量）")
    priority: str
    weight: int
    status: str
    source: str = Field(description="既有工單／系統開立")
    release_on: str
    due_on: str
    release_min: int
    due_min: int
    note: str | None = None
    n_ops: int
    work_min: int = Field(description="自製工序的準備＋加工分鐘合計")


class PlannedWorkOrder(BaseModel):
    wo_no: str
    part_id: str
    part_name: str
    qty: int
    priority: str
    source: str
    status: str
    due_on: str
    due_min: int
    release_min: int
    start_min: int | None
    end_min: int | None
    start_at: str | None
    end_at: str | None = Field(description="完工時間（含委外）")
    late_min: int | None = Field(description="延遲的工作分鐘；0＝準時")
    on_time: bool | None


class SkippedWorkOrder(BaseModel):
    wo_no: str
    part_id: str
    reason: str


class PartPlanWorkOrder(ScheduleWorkOrder):
    plan: PlannedWorkOrder | None = Field(description="目前排程中的完工時間；尚未排程為 null")


class WorkOrderSuggestion(BaseModel):
    qty: int
    due_on: str
    priority: Literal["一般", "急件"]
    reason: str


class PartPlan(BaseModel):
    part_id: str
    part_name: str
    plan_start: str
    has_routing: bool
    routing: list[RoutingOp]
    suggestion: WorkOrderSuggestion
    work_orders: list[PartPlanWorkOrder]
    skipped: list[SkippedWorkOrder] = Field(description="不排程的工單（委外處理中）")
    schedule_run_id: str | None


class WorkOrderCreate(BaseModel):
    part_id: str
    qty: int = Field(ge=1, le=5000)
    due_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="交期（當天下班前完工算準時）")
    priority: Literal["一般", "急件"] = "一般"
    release_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str | None = Field(default=None, max_length=200)


class WorkOrderCreated(BaseModel):
    wo_no: str
    part_id: str
    part_name: str
    qty: int
    priority: str
    release_on: str
    due_on: str
    note: str | None
    status: str
    created_at: str
    source: str


class ScheduledOp(BaseModel):
    op_id: str
    wo_no: str
    part_id: str
    op_seq: int
    op_name: str
    kind: Literal["自製", "委外"]
    machine_id: str | None = Field(description="委外為 null")
    start_min: int = Field(description="工作分鐘（排程起點起算，只計上班時間）")
    end_min: int
    setup_min: int
    run_min: int
    start_at: str
    end_at: str
    pinned: bool = Field(description="生產中、釘選在機台最前面的工序")


class ScheduleKpis(BaseModel):
    n_work_orders: int
    n_late: int
    on_time_rate: float | None
    total_late_min: int
    total_setup_min: int
    n_setups: int
    makespan_min: int
    finish_at: str | None
    utilization: dict[str, float] = Field(description="各機台忙碌時間 ÷ 最後一道自製工序完成時間")


class ConstraintScore(BaseModel):
    constraint: str
    level: Literal["hard", "medium", "soft"]
    score: int
    matches: int
    description: str


class AxisDay(BaseModel):
    date: str
    weekday: str
    start_min: int
    holidays_before: list[dict]


class ScheduleRunRow(BaseModel):
    run_id: str
    created_at: str
    engine: str
    engine_version: str | None
    status: str
    seconds_limit: int | None
    solve_ms: int | None
    score: str | None
    hard: int | None
    medium: int | None
    soft: int | None
    initial_score: str | None
    improvements: int | None
    n_work_orders: int | None
    n_operations: int | None
    kpis: ScheduleKpis
    score_check: bool | None = Field(
        default=None, description="後端依同一套規則重算的總分是否與 Timefold 一致；簡易排程為 null"
    )


class ScheduleRunDetail(ScheduleRunRow):
    request_id: str | None
    engine_label: str
    note: str | None
    analysis: list[ConstraintScore]
    work_orders: list[PlannedWorkOrder]
    operations: list[ScheduledOp]
    axis: list[AxisDay]
    missing: list[str] = Field(description="目前有、但這次排程沒有的工單（之後才開立）")
    removed: list[str] = Field(description="這次排程有、但已取消或完工的工單")


class MachineInfo(BaseModel):
    machine_id: str
    name: str
    machine_type: str
    site: str


class ProductionCalendar(BaseModel):
    shifts: list[list[str]]
    workdays: list[int]
    holidays: list[dict]
    day_minutes: int


class ProductionOverview(BaseModel):
    plan_start: str
    calendar: ProductionCalendar
    priority_weights: dict[str, int]
    machine_types: list[dict]
    machines: list[MachineInfo]
    work_orders: list[ScheduleWorkOrder]
    skipped: list[SkippedWorkOrder]
    problem: dict
    engine: SchedulerEngine
    current: ScheduleRunDetail | None
    runs: list[ScheduleRunRow]
    solving: bool
    settings: dict


class ScheduleSolveRequest(BaseModel):
    seconds: int | None = Field(default=None, ge=3, le=60, description="求解秒數；預設 20")
    engine: Literal["timefold", "greedy"] = Field(
        default="timefold", description="greedy＝簡易排程（交期優先派工），用來和 Timefold 比較"
    )


# ---------------------------------------------------------------- 智慧助理（docs/adr/011）
class Account(BaseModel):
    id: str
    label: str
    role: str
    role_label: str
    ops: list[str]
    warehouses: list[str]
    customers: list[str]
    note: str


class AccountsResponse(BaseModel):
    current: Account
    accounts: list[Account]
    demo_controls: bool
    pending_approvals: int = Field(description="待核准單數量（主管看得到要處理幾件）")


class SwitchAccountRequest(BaseModel):
    account_id: str


class System1Status(BaseModel):
    jev_configured: bool
    detail: str
    model: str
    timeout_s: float
    thresholds: dict[str, float]
    clarify_margin: float


class RouteRequest(BaseModel):
    question: str = Field(default="", max_length=300)
    image_id: str | None = None
    forced_intent: str | None = Field(default=None, description="使用者點澄清按鈕選的意圖")
    engine: Literal["auto", "jev", "local"] = Field(
        default="auto", description="auto：有金鑰用 Jev、否則本地；eval-route 用 jev／local 比較"
    )


class RankedIntent(BaseModel):
    intent: str
    label: str
    prob: float


class RouteEgress(BaseModel):
    bytes: int = Field(description="送出本機的位元組數（Jev 請求本文）；本地路由為 0")
    to: str | None
    images: int = 0


class RoutePhoto(BaseModel):
    kind: Literal["art", "drawing", "unknown"]
    id: str | None
    label: str


class RouteResponse(BaseModel):
    request_id: str
    account: Account
    question: str
    masked_text: str = Field(description="送 Jev 的代號化文字（本地路由時只在本機）")
    mapping: dict[str, dict] = Field(description="代號 → 實體（只留在本機）")
    entities: list[dict]
    photo: RoutePhoto | None
    engine: Literal["jev", "local", "user"]
    engine_label: str
    model: str
    fallback_reason: str | None
    intent: str
    intent_label: str
    risk: Literal["read", "heavy", "write"]
    confidence: float
    margin: float
    jev_confidence: float | None
    ranked: list[RankedIntent]
    modify_op: str | None
    flags: dict[str, bool]
    gate: Literal["direct", "confirm", "modify", "clarify", "out_of_scope"]
    threshold: float
    gate_reason: str
    options: list[RankedIntent]
    permitted: bool
    permission_note: str
    dispatch: dict
    egress: RouteEgress
    latency_ms: dict[str, int]
    detail: dict
    jev_request: dict | None = Field(description="實際送給 Jev 的請求本文（畫面上可展開檢查）")


class ChangeCheck(BaseModel):
    key: Literal["role", "scope", "field", "limit"]
    label: str
    ok: bool | None = Field(description="null＝前一項已不符，未檢查")
    detail: str


class ChangeDiff(BaseModel):
    label: str
    field: str
    before: str | int | float | None
    after: str | int | float | None


class ChangePreviewRequest(BaseModel):
    question: str | None = Field(default=None, max_length=300)
    op: str | None = Field(default=None, description="路由判斷的操作；表單送出時必填")
    params: dict | None = Field(default=None, description="表單送出時的參數（不經參數抽取）")


class ChangePreview(BaseModel):
    request_id: str
    op: str
    op_label: str
    account: Account
    params: dict
    param_labels: dict
    sources: dict[str, str]
    notes: list[str]
    llm: dict | None
    missing: list[str]
    checks: list[ChangeCheck]
    diff: list[ChangeDiff]
    reasons: list[str] = Field(description="超過額度的原因（非空＝要送主管核准）")
    pending_id: str | None
    summary: str
    next: Literal["confirm", "approval", "rejected", "need_info"]
    message: str
    latency_ms: int | None = None


class ChangeCommitted(BaseModel):
    change_no: str
    text: str = Field(description="依資料庫讀回結果套固定模板的回覆")
    rows: list[ChangeDiff]
    moves: int
    op: str
    summary: str
    account: Account


class ApprovalRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class ReturnRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class Approval(BaseModel):
    ap_no: str
    op: str
    op_label: str
    params: dict
    param_labels: dict
    summary: str
    reasons: list[str]
    diff: list[ChangeDiff]
    note: str | None
    requester_id: str
    requester_label: str
    created_at: str
    status: Literal["待核准", "已核准", "已退回", "已失效"]
    decided_label: str | None
    decided_at: str | None
    decision_note: str | None
    change_no: str | None


class ApprovalsResponse(BaseModel):
    can_approve: bool
    pending: list[Approval]
    mine: list[Approval]
    recent: list[Approval]


class ApprovalDecision(BaseModel):
    ap_no: str
    status: str
    text: str
    change_no: str | None = None
    rows: list[ChangeDiff] = []
    moves: int = 0


class RouteEvalRunsResponse(BaseModel):
    runs: list[dict] = Field(description="eval/runs/*-route.json 的摘要（新到舊）")


class AuditRow(BaseModel):
    id: int
    at: str
    actor_id: str
    actor_label: str
    action: str
    op: str | None
    ref_no: str | None
    summary: str | None
    detail: dict | list | str | None
    request_id: str | None


class AuditResponse(BaseModel):
    items: list[AuditRow]
    changes: list[dict] = Field(description="最近寫入的異動單")


# ---------------------------------------------------------------- 照片建檔（docs/adr/013）
class IntakeRequest(BaseModel):
    image_id: str = Field(description="上傳的照片（POST /images）")
    domain: Literal["mfg", "art"] | None = Field(
        default=None,
        description="從哪一邊進來：mfg＝工廠圖紙、art＝畫作；null＝交給領域路由判斷。"
        "指定了但路由很確定是另一個領域時回 INTAKE_WRONG_DOMAIN",
    )


class IntakeField(BaseModel):
    key: str
    label: str
    value: str | float | None
    source: Literal["Qwen3-VL", "規則", "人"] | None = Field(
        description="Qwen3-VL＝從照片讀的；規則＝依知識庫校正或補上的；人＝在確認頁或表單填的"
    )
    note: str | None = Field(default=None, description="規則改了什麼")
    hint: str | None = Field(default=None, description="輸入提示（人填的欄位）")
    group: str | None = Field(default=None, description="表單分區（畫作的跳出表單）")
    read: bool = Field(description="從照片讀的欄位；false＝照片上沒有，由人填")
    required: bool = Field(
        description="必填；畫作的條件必填（CC BY 4.0 的標示文字、填了介紹的出處）也算"
    )
    kind: Literal["text", "number", "enum", "url", "longtext"]
    options: list[str] = Field(default=[], description="列舉值（kind＝enum）")
    suggestions: list[str] = Field(default=[], description="知識庫既有零件用過的值")
    status: Literal["ok", "invalid", "missing", "empty"] = Field(
        description="invalid、missing 要處理完才能收錄；empty＝選填沒填"
    )
    message: str | None = None


class IntakeCheck(BaseModel):
    label: str
    ok: bool
    detail: str


class IntakeExtraction(BaseModel):
    model: str | None = Field(description="讀標題欄的模型；沒讀（mock、模型無法使用）為 null")
    strategy: str | None
    ms: int | None
    raw: str | None = Field(description="模型的原始輸出（前 1,000 字）")
    error: str | None
    tokens: dict[str, int] | None = None


class IntakeCommit(BaseModel):
    by: str
    by_label: str
    at: str
    kb_version: str = Field(description="收錄後的 kb/VERSION")
    index_ms: int | None = Field(description="重建索引花的時間；還在重建為 null")
    error: str | None = Field(description="收錄失敗的原因（寫進去的檔案與版本已還原）")


class IntakeDraft(BaseModel):
    draft_id: str
    domain: Literal["mfg", "art"]
    status: Literal["draft", "indexing", "done", "failed"] = Field(
        description="indexing＝已寫進 kb/、背景重建索引中；failed＝收錄失敗、已還原，可以改了再收錄"
    )
    created_at: str
    updated_at: str | None
    image_id: str
    photo_url: str
    kb_image_url: str = Field(
        description="要存進知識庫的圖：圖紙是拉正、對齊版面後的圖（kb/drawings），畫作是照片本身（kb/images）"
    )
    item_id: str = Field(
        description="收錄後的 ID：圖紙是預定的編號（收錄時再確認一次）；畫作是「來源代碼－編號」"
    )
    fields: list[IntakeField]
    checks: list[IntakeCheck]
    extraction: IntakeExtraction = Field(description="讀標題欄的結果；畫作不讀照片，全為 null")
    can_commit: bool = Field(description="欄位都通過驗證（還要有 kb_intake 權限，只有主管）")
    blockers: list[str] = Field(description="還沒通過驗證的欄位")
    commit: IntakeCommit | None
    item_url: str | None = Field(description="收錄完成後的圖紙頁或畫作頁（前端路由）")
    egress: dict[str, int]


class IntakeUpdate(BaseModel):
    values: dict[str, str | float | None] = Field(
        description="要修改的欄位（key → 值，null＝清空）；改過的欄位來源標成「人」"
    )


HealthResponse.model_rebuild()
