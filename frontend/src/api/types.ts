// 後端 OpenAPI 型別的別名（schema.d.ts 由 `npm run gen:api` 從 shared/openapi.json 產生）
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type ArtworkSummary = Schemas["ArtworkSummary"];
export type ArtworkDetail = Schemas["ArtworkDetail"];
export type ColorAnalysis = Schemas["ColorAnalysis"];
export type StyleGuess = Schemas["StyleGuess"];
export type StyleField = Schemas["StyleField"];
export type ImageAlignment = Schemas["ImageAlignment"];
export type ImageSearchResponse = Schemas["ImageSearchResponse"];
export type TextSearchResponse = Schemas["TextSearchResponse"];
export type HealthResponse = Schemas["HealthResponse"];
/** 登入後畫面用的系統狀態（docs/adr/030）：服務能不能用、記憶體、展示模式；不含模型端點與紀錄 */
export type StatusResponse = Schemas["StatusResponse"];
/** 管理診斷（只有 access.yaml 的 views.diagnostics 看得到，預設主管） */
export type DiagnosticsResponse = Schemas["DiagnosticsResponse"];
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
export type TokenInfo = Schemas["TokenInfo"];
export type RouteRequest = Pick<Schemas["RouteRequest"], "question"> & Partial<Omit<Schemas["RouteRequest"], "question">>;
export type RouteResponse = Schemas["RouteResponse"];
export type SecurityLogsResponse = Schemas["SecurityLogsResponse"];
export type ChangePreview = Schemas["ChangePreview"];
export type ChangePreviewRequest = Schemas["ChangePreviewRequest"];
export type ChangeCommitted = Schemas["ChangeCommitted"];
export type ChangeCheck = Schemas["ChangeCheck"];
export type ChangeDiff = Schemas["ChangeDiff"];
export type Approval = Schemas["Approval"];
export type ApprovalsResponse = Schemas["ApprovalsResponse"];
export type ApprovalDecision = Schemas["ApprovalDecision"];
export type AuditResponse = Schemas["AuditResponse"];
// 照片建檔（docs/adr/013）
export type IntakeDraft = Schemas["IntakeDraft"];
export type RegionDraft = Schemas["RegionDraft"];
export type RegionDraftsResponse = Schemas["RegionDraftsResponse"];
export type RegionDraftRequest = Schemas["RegionDraftRequest"];
export type IntakeField = Schemas["IntakeField"];
export type IntakeCheck = Schemas["IntakeCheck"];
// 批次辨識、兩件並排比較、匯出（docs/adr/017）
export type ItemComparison = Schemas["ItemComparison"];
export type CompareRow = Schemas["CompareRow"];
export type ExportAuditRequest = Pick<Schemas["ExportAuditRequest"], "kind"> &
  Partial<Omit<Schemas["ExportAuditRequest"], "kind">>;
type ChatDefaults = "strategy" | "use_retrieval" | "allow_fallback";
/** 有預設值的欄位在請求時可省略 */
export type ChatRequest = Omit<Schemas["ChatRequest"], ChatDefaults> &
  Partial<Pick<Schemas["ChatRequest"], ChatDefaults>>;
export type Strategy = Schemas["ChatRequest"]["strategy"];
// 背景工作（TASK-20261013-001）
export type Job = Schemas["Job"];
export type JobCreated = Schemas["JobCreated"];
export type JobStatus = Job["status"];
export type CadJobCreate = Partial<Pick<Schemas["CadJobCreate"], "part_id" | "image_id" | "strategy">>;
export type ScheduleJobCreate = Partial<Schemas["ScheduleJobCreate"]>;
