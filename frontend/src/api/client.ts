// 前端統一的 API 入口（共用層 §八）：元件、頁面與 hook 只經過這裡呼叫後端，不直接 fetch、
// 不直接連模型、資料庫、Redis 或 solver。
//
//   http.ts       共用底層：憑證（HttpOnly cookie、401 自動重新取得）、統一錯誤、Idempotency-Key
//   sse.ts        SSE 解析（問答、庫存查詢、批次；背景工作的事件串流含 Last-Event-ID 續接）
//   types.ts      OpenAPI 產生的型別（schema.d.ts）別名
//   五項功能的 API 模組（同一個 /api/v1 入口）：
//     artworks.ts   畫作圖紙      drawings.ts   工廠圖紙      inventory.ts  工廠資料庫（含異動與核准）
//     cad.ts        工廠 3D 重建  schedule.ts    工廠工單排程
//   jobs.ts       背景工作（3D 重建與排程共用）：狀態、取消、事件追蹤
//   common.ts     共用：上傳、身分、智慧助理、系統與稽核
export * from "./http";
export * from "./types";
export { workOrderScope } from "./schedule";

import { artApi } from "./artworks";
import { cadApi } from "./cad";
import { commonApi } from "./common";
import { drawingsApi } from "./drawings";
import { inventoryApi } from "./inventory";
import { jobsApi } from "./jobs";
import { scheduleApi } from "./schedule";

export const api = {
  ...commonApi,
  ...artApi,
  ...drawingsApi,
  ...inventoryApi,
  ...cadApi,
  ...scheduleApi,
  ...jobsApi,
};
