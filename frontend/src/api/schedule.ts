// 工廠工單排程：工單管理、排程背景工作（POST /schedule/jobs → 202）、提前結束、結果
import { idemHeaders, json, request, withIdempotency } from "./http";
import type { JobCreated, PartPlan, ProductionOverview, ScheduleJobCreate, ScheduleRunDetail, Schemas, WorkOrderCreate, WorkOrderCreated } from "./types";
import { trackWrite } from "./writes";

/** 某個零件「開工單」這件事（直接開立或送主管核准）的寫入範圍 */
export const workOrderScope = (partId: string) => `work-order:create:${partId}`;

export const scheduleApi = {
  productionOverview: () => request<ProductionOverview>("/production/overview"),
  partPlan: (id: string) => request<PartPlan>(`/production/parts/${encodeURIComponent(id)}`),
  // 同一個零件開工單（直接開，或超過額度送核准）是同一件事：上一筆結果未確認前不能再送（見 writes.ts）；
  // 同樣的內容重送帶同一個 Idempotency-Key，伺服器不會多開
  createWorkOrder: (body: WorkOrderCreate) =>
    trackWrite(
      "開立工單",
      () =>
        withIdempotency(`work-order:${JSON.stringify(body)}`, (key) =>
          request<WorkOrderCreated>("/production/work-orders", { ...json(body), headers: { "Content-Type": "application/json", ...idemHeaders(key) } }),
        ),
      workOrderScope(body.part_id),
    ),
  cancelWorkOrder: (woNo: string) =>
    trackWrite(
      "取消工單",
      () => withIdempotency(`cancel-wo:${woNo}`, (key) => request<Schemas["OkResponse"]>(`/production/work-orders/${encodeURIComponent(woNo)}`, { method: "DELETE", headers: idemHeaders(key) })),
      `work-order:cancel:${woNo}`,
    ),
  /** 建立排程背景工作：只有生管；已有排隊或執行中的 409 SCHEDULE_BUSY；帶 input_version 過期 409 SCHEDULE_INPUT_CHANGED */
  createScheduleJob: (body: ScheduleJobCreate, key?: string) => {
    const init = json(body);
    return request<JobCreated>("/schedule/jobs", key ? { ...init, headers: { ...init.headers, ...idemHeaders(key) } } : init);
  },
  /** 提前結束：採用目前最佳的合法解並照常驗證、寫回（和取消不同，取消是放棄、不寫回） */
  stopSchedule: (jobId?: string) => request<Schemas["OkResponse"]>("/schedule/stop", jobId ? json({ job_id: jobId }) : { method: "POST" }),
  scheduleRun: (runId: string) => request<ScheduleRunDetail>(`/schedule/runs/${encodeURIComponent(runId)}`),
  resetProduction: () => request<Schemas["OkResponse"]>("/admin/production/reset", { method: "POST" }),
};
