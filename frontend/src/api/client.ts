// 所有 API 呼叫集中在這裡；元件不直接寫 fetch（共用層 §八）
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type ArtworkSummary = Schemas["ArtworkSummary"];
export type ArtworkDetail = Schemas["ArtworkDetail"];
export type ColorAnalysis = Schemas["ColorAnalysis"];
export type ImageSearchResponse = Schemas["ImageSearchResponse"];
export type TextSearchResponse = Schemas["TextSearchResponse"];
export type HealthResponse = Schemas["HealthResponse"];
export type EvalRunsResponse = Schemas["EvalRunsResponse"];
export type PartSummary = Schemas["PartSummary"];
export type PartDetail = Schemas["PartDetail"];
export type DrawingSearchResponse = Schemas["DrawingSearchResponse"];
export type AnySearchResponse = Schemas["AnySearchResponse"];
export type RouteInfo = Schemas["RouteInfo"];
export type PartTextSearchResponse = Schemas["PartTextSearchResponse"];
export type ReconstructRequest = Schemas["ReconstructRequest"];
export type CadStrategy = NonNullable<ReconstructRequest["strategy"]>;
export type InventorySchema = Schemas["InventorySchemaResponse"];
export type InventoryOverviewRow = Schemas["InventoryOverviewRow"];
export type PartInventory = Schemas["PartInventory"];
export type InventoryAskRequest = Pick<Schemas["InventoryAskRequest"], "question"> &
  Partial<Omit<Schemas["InventoryAskRequest"], "question">>;
// 生產排程（Timefold）與記憶體管理
export type ProductionOverview = Schemas["ProductionOverview"];
export type PartPlan = Schemas["PartPlan"];
export type WorkOrderCreate = Pick<Schemas["WorkOrderCreate"], "part_id" | "qty" | "due_on"> &
  Partial<Omit<Schemas["WorkOrderCreate"], "part_id" | "qty" | "due_on">>;
export type WorkOrderCreated = Schemas["WorkOrderCreated"];
export type ScheduleWorkOrder = Schemas["ScheduleWorkOrder"];
export type PlannedWorkOrder = Schemas["PlannedWorkOrder"];
export type ScheduledOp = Schemas["ScheduledOp"];
export type ScheduleKpis = Schemas["ScheduleKpis"];
export type ConstraintScore = Schemas["ConstraintScore"];
export type AxisDay = Schemas["AxisDay"];
export type ScheduleRunDetail = Schemas["ScheduleRunDetail"];
export type SchedulerEngine = Schemas["SchedulerEngine"];
export type ScheduleSolveRequest = Partial<Schemas["ScheduleSolveRequest"]>;
export type MemoryStatus = Schemas["MemoryStatus"];
export type MemoryEvent = Schemas["MemoryEvent"];
// 智慧助理（System 1 路由、修改資料、主管核准）
export type Account = Schemas["Account"];
export type AccountsResponse = Schemas["AccountsResponse"];
export type RouteRequest = Pick<Schemas["RouteRequest"], "question"> & Partial<Omit<Schemas["RouteRequest"], "question">>;
export type RouteResponse = Schemas["RouteResponse"];
export type SecurityLogsResponse = Schemas["SecurityLogsResponse"];
/** 第 2、4 段由誰判斷：雲端 Jev（只收代號化文字）或地端規則 */
export type GuardEngine = "jev" | "local";
export type ChangePreview = Schemas["ChangePreview"];
export type ChangePreviewRequest = Schemas["ChangePreviewRequest"];
export type ChangeCommitted = Schemas["ChangeCommitted"];
export type ChangeCheck = Schemas["ChangeCheck"];
export type ChangeDiff = Schemas["ChangeDiff"];
export type Approval = Schemas["Approval"];
export type ApprovalsResponse = Schemas["ApprovalsResponse"];
export type ApprovalDecision = Schemas["ApprovalDecision"];
export type AuditResponse = Schemas["AuditResponse"];
type ChatDefaults = "strategy" | "use_retrieval" | "allow_fallback";
/** 有預設值的欄位在請求時可省略 */
export type ChatRequest = Omit<Schemas["ChatRequest"], ChatDefaults> &
  Partial<Pick<Schemas["ChatRequest"], ChatDefaults>>;
export type Strategy = Schemas["ChatRequest"]["strategy"];

export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId: string,
    public status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1${path}`, init);
  } catch {
    throw new ApiError("NETWORK_ERROR", "連不上伺服器，請確認網路或後端是否啟動", "", 0);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(
      body?.error?.code ?? `HTTP_${res.status}`,
      body?.error?.message ?? res.statusText,
      body?.error?.request_id ?? res.headers.get("X-Request-ID") ?? "",
      res.status,
    );
  }
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** 後端回傳的圖片路徑（/api/v1/...）加上 API_BASE */
export const assetUrl = (path: string) => `${API_BASE}${path}`;

export const api = {
  uploadImage(file: Blob) {
    const form = new FormData();
    form.append("file", file, "photo.jpg");
    return request<Schemas["ImageUploadResponse"]>("/images", { method: "POST", body: form });
  },
  uploadedImageUrl: (imageId: string) => `${API_BASE}/api/v1/images/${imageId}`,
  searchImage: (imageId: string) =>
    request<ImageSearchResponse>("/search/image", json({ image_id: imageId })),
  /** 不指定領域：後端先判斷是畫作還是工廠圖紙（領域路由），再做該領域的辨識 */
  searchAny: (imageId: string) =>
    request<AnySearchResponse>("/search/any", json({ image_id: imageId })),
  searchText: (q: string) =>
    request<TextSearchResponse>(`/search/text?q=${encodeURIComponent(q)}`),
  listArtworks: () => request<Schemas["ArtworkListResponse"]>("/artworks"),
  getArtwork: (id: string) => request<ArtworkDetail>(`/artworks/${encodeURIComponent(id)}`),
  /** 色彩分析（docs/adr/010）：知識庫畫作讀索引算好的結果，上傳照片即時計算 */
  artworkColors: (id: string) => request<ColorAnalysis>(`/artworks/${encodeURIComponent(id)}/colors`),
  photoColors: (imageId: string) =>
    request<ColorAnalysis>(`/images/${encodeURIComponent(imageId)}/colors`),
  health: () => request<HealthResponse>("/health"),
  evalRuns: () => request<EvalRunsResponse>("/eval/runs"),
  feedback: (body: Schemas["FeedbackRequest"]) => request<Schemas["OkResponse"]>("/feedback", json(body)),
  setOutage: (enabled: boolean) => request<Schemas["OkResponse"]>("/admin/outage", json({ enabled })),
  // 工廠機械加工圖
  listParts: () => request<Schemas["PartListResponse"]>("/parts"),
  getPart: (id: string) => request<PartDetail>(`/parts/${encodeURIComponent(id)}`),
  searchDrawing: (imageId: string) =>
    request<DrawingSearchResponse>("/search/drawing", json({ image_id: imageId })),
  searchParts: (q: string) => request<PartTextSearchResponse>(`/search/parts?q=${encodeURIComponent(q)}`),
  cadJob: (jobId: string) => request<Schemas["CadJobSummary"]>(`/cad/jobs/${encodeURIComponent(jobId)}`),
  partReconstructions: (id: string) =>
    request<Schemas["CadJobListResponse"]>(`/parts/${encodeURIComponent(id)}/reconstructions`),
  cadEvalRuns: () => request<Schemas["CadEvalRunsResponse"]>("/eval/cad-runs"),
  // 工廠庫存（Text-to-SQL）
  inventorySchema: () => request<InventorySchema>("/inventory/schema"),
  inventoryOverview: () => request<Schemas["InventoryOverviewResponse"]>("/inventory/overview"),
  partInventory: (id: string) => request<PartInventory>(`/inventory/parts/${encodeURIComponent(id)}`),
  sqlEvalRuns: () => request<Schemas["SqlEvalRunsResponse"]>("/eval/sql-runs"),
  // 生產排程（Timefold）
  productionOverview: () => request<ProductionOverview>("/production/overview"),
  partPlan: (id: string) => request<PartPlan>(`/production/parts/${encodeURIComponent(id)}`),
  createWorkOrder: (body: WorkOrderCreate) => request<WorkOrderCreated>("/production/work-orders", json(body)),
  cancelWorkOrder: (woNo: string) =>
    request<Schemas["OkResponse"]>(`/production/work-orders/${encodeURIComponent(woNo)}`, { method: "DELETE" }),
  stopSchedule: () => request<Schemas["OkResponse"]>("/schedule/stop", { method: "POST" }),
  resetProduction: () => request<Schemas["OkResponse"]>("/admin/production/reset", { method: "POST" }),
  // 記憶體管理
  memory: () => request<MemoryStatus>("/memory"),
  releaseMemory: () => request<Schemas["MemoryReleaseResponse"]>("/admin/memory/release", { method: "POST" }),
  // 智慧助理：身分（存在伺服器端的工作階段，cookie 由瀏覽器自動帶）、路由、修改資料、主管核准
  accounts: () => request<AccountsResponse>("/auth/accounts"),
  switchAccount: (accountId: string) => request<AccountsResponse>("/auth/switch", json({ account_id: accountId })),
  route: (body: RouteRequest) => request<RouteResponse>("/agent/route", json(body)),
  changePreview: (body: ChangePreviewRequest) => request<ChangePreview>("/changes/preview", json(body)),
  changeCommit: (pendingId: string) =>
    request<ChangeCommitted>(`/changes/${encodeURIComponent(pendingId)}/commit`, { method: "POST" }),
  requestApproval: (pendingId: string, note?: string) =>
    request<Approval>(`/changes/${encodeURIComponent(pendingId)}/request-approval`, json({ note: note || null })),
  approvals: () => request<ApprovalsResponse>("/approvals"),
  approve: (apNo: string, note?: string) =>
    request<ApprovalDecision>(`/approvals/${encodeURIComponent(apNo)}/approve`, json({ note: note || null })),
  returnApproval: (apNo: string, reason: string) =>
    request<ApprovalDecision>(`/approvals/${encodeURIComponent(apNo)}/return`, json({ reason })),
  audit: () => request<AuditResponse>("/audit"),
  securityLogs: (limit = 20) => request<SecurityLogsResponse>(`/security/logs?limit=${limit}`),
  routeEvalRuns: () => request<Schemas["RouteEvalRunsResponse"]>("/eval/route-runs"),
};
