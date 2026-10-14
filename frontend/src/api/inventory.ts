// 工廠資料庫：固定查詢、Text-to-SQL（sse.ts 的 streamInventoryAsk）與資料異動／主管核准
import { idemHeaders, json, request, withIdempotency } from "./http";
import type {
  Approval,
  ApprovalDecision,
  ApprovalsResponse,
  ChangeCommitted,
  ChangePreview,
  ChangePreviewRequest,
  InventorySchema,
  PartInventory,
  Schemas,
} from "./types";
import { trackWrite } from "./writes";

const post = (body: unknown, key?: string): RequestInit => {
  const init = json(body);
  return key ? { ...init, headers: { ...init.headers, ...idemHeaders(key) } } : init;
};

export const inventoryApi = {
  inventorySchema: () => request<InventorySchema>("/inventory/schema"),
  inventoryOverview: () => request<Schemas["InventoryOverviewResponse"]>("/inventory/overview"),
  partInventory: (id: string) => request<PartInventory>(`/inventory/parts/${encodeURIComponent(id)}`),
  sqlEvalRuns: () => request<Schemas["SqlEvalRunsResponse"]>("/eval/sql-runs"),
  changePreview: (body: ChangePreviewRequest) => request<ChangePreview>("/changes/preview", json(body)),
  // 寫入一律經 trackWrite：送出後就算不再接收結果，伺服器也可能已經完成（見 writes.ts）；
  // 同一個使用者動作的重送共用一個 Idempotency-Key，伺服器只寫入一次。
  // 對話裡的確認寫入／送主管核准另有「已送出」標記（shell/persist.ts）；功能頁的送核准帶 scope（重新試算會拿到新的 pending_id，
  // 所以 scope 用「這件事」而不是 pending_id）
  changeCommit: (pendingId: string, scope?: string) =>
    trackWrite(
      "確認寫入",
      () => withIdempotency(`commit:${pendingId}`, (key) => request<ChangeCommitted>(`/changes/${encodeURIComponent(pendingId)}/commit`, { method: "POST", headers: idemHeaders(key) })),
      scope,
    ),
  requestApproval: (pendingId: string, note?: string, scope?: string) =>
    trackWrite(
      "送主管核准",
      () => withIdempotency(`request-approval:${pendingId}`, (key) => request<Approval>(`/changes/${encodeURIComponent(pendingId)}/request-approval`, post({ note: note || null }, key))),
      scope,
    ),
  approvals: () => request<ApprovalsResponse>("/approvals"),
  approve: (apNo: string, note?: string) =>
    trackWrite(
      "核准",
      () => withIdempotency(`approve:${apNo}`, (key) => request<ApprovalDecision>(`/approvals/${encodeURIComponent(apNo)}/approve`, post({ note: note || null }, key))),
      `approval:${apNo}`,
    ),
  returnApproval: (apNo: string, reason: string) =>
    trackWrite("退回", () => request<ApprovalDecision>(`/approvals/${encodeURIComponent(apNo)}/return`, json({ reason })), `approval:${apNo}`),
};
