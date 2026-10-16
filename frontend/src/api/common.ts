// 共用層：上傳、身分、智慧助理路由、系統狀態、稽核、照片建檔、批次與比較、匯出
import { API_BASE, json, request } from "./http";
import type {
  AccountsResponse,
  AnySearchResponse,
  DiagnosticsResponse,
  EvalRunsResponse,
  ExportAuditRequest,
  HealthResponse,
  IntakeDraft,
  ItemComparison,
  MemoryStatus,
  RouteRequest,
  RouteResponse,
  Schemas,
  SecurityLogsResponse,
  StatusResponse,
  AuditResponse,
} from "./types";
import { trackWrite } from "./writes";

export const commonApi = {
  uploadImage(file: Blob) {
    const form = new FormData();
    form.append("file", file, "photo.jpg");
    return request<Schemas["ImageUploadResponse"]>("/images", { method: "POST", body: form });
  },
  uploadedImageUrl: (imageId: string) => `${API_BASE}/api/v1/images/${imageId}`,
  /** 擁有者明確授權其他帳號使用這張照片（整份取代；空陣列＝只有自己） */
  setImageAcl: (imageId: string, accounts: string[]) =>
    request<Schemas["ImageAcl"]>(`/images/${encodeURIComponent(imageId)}/acl`, { ...json({ accounts }), method: "PUT" }),
  /** 不指定領域：後端先判斷是畫作還是工廠圖紙（領域路由），再做該領域的辨識 */
  searchAny: (imageId: string) => request<AnySearchResponse>("/search/any", json({ image_id: imageId })),
  health: () => request<HealthResponse>("/health"),
  status: () => request<StatusResponse>("/status"),
  diagnostics: () => request<DiagnosticsResponse>("/admin/diagnostics"),
  evalRuns: () => request<EvalRunsResponse>("/eval/runs"),
  feedback: (body: Schemas["FeedbackRequest"]) => request<Schemas["OkResponse"]>("/feedback", json(body)),
  setOutage: (enabled: boolean) => request<Schemas["OkResponse"]>("/admin/outage", json({ enabled })),
  // 記憶體管理
  memory: () => request<MemoryStatus>("/memory"),
  releaseMemory: () => request<Schemas["MemoryReleaseResponse"]>("/admin/memory/release", { method: "POST" }),
  // 智慧助理：身分（JWT，放在 HttpOnly cookie，瀏覽器自動帶）、路由
  accounts: () => request<AccountsResponse>("/auth/accounts"),
  switchAccount: (accountId: string) => request<AccountsResponse>("/auth/switch", json({ account_id: accountId })),
  /** token：示範「竄改過的憑證」時才給（放在 Authorization 標頭，閘道優先看它） */
  route: (body: RouteRequest, token?: string, signal?: AbortSignal) => {
    const init: RequestInit = { ...json(body), signal };
    if (token) init.headers = { ...init.headers, Authorization: `Bearer ${token}` };
    return request<RouteResponse>("/agent/route", init, !!token);
  },
  securityLogs: (limit = 20) => request<SecurityLogsResponse>(`/security/logs?limit=${limit}`),
  audit: () => request<AuditResponse>("/audit"),
  routeEvalRuns: () => request<Schemas["RouteEvalRunsResponse"]>("/eval/route-runs"),
  // 照片建檔（docs/adr/013）：建立草稿走 SSE（sse.ts 的 streamIntake）
  intakeDraft: (draftId: string) => request<IntakeDraft>(`/intake/${encodeURIComponent(draftId)}`),
  updateIntake: (draftId: string, values: Record<string, string | number | null>) =>
    request<IntakeDraft>(`/intake/${encodeURIComponent(draftId)}`, { ...json({ values }), method: "PUT" }),
  commitIntake: (draftId: string) =>
    trackWrite("照片建檔入庫", () => request<IntakeDraft>(`/intake/${encodeURIComponent(draftId)}/commit`, { method: "POST" }), `intake:${draftId}`),
  discardIntake: (draftId: string) => request<Schemas["OkResponse"]>(`/intake/${encodeURIComponent(draftId)}`, { method: "DELETE" }),
  // 批次辨識走 SSE（sse.ts 的 streamBatch）；兩件並排比較：a、b 是 artwork:<id> 或 part:<id>
  compareItems: (a: string, b: string) => request<ItemComparison>(`/compare/items?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  /** 匯出 CSV、比較表、問答報告時記一筆稽核（檔案在瀏覽器裡產生） */
  logExport: (body: ExportAuditRequest) => request<Schemas["OkResponse"]>("/exports", json(body)),
};
