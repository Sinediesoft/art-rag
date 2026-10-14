// 工廠 3D 重建：圖紙 → 零件模型。新流程是背景工作（POST /cad/jobs → 202，事件在 jobs.ts 的 followJob），
// 舊結果的相容讀取仍用 cadJob。
import { idemHeaders, json, request } from "./http";
import type { CadJobCreate, JobCreated, Schemas } from "./types";

export const cadApi = {
  /** 建立 3D 重建背景工作（gpu 佇列）：授權通過才回 202；同一個 key 的重送拿到同一個工作 */
  createCadJob: (body: CadJobCreate, key?: string) => {
    const init = json(body);
    return request<JobCreated>("/cad/jobs", key ? { ...init, headers: { ...init.headers, ...idemHeaders(key) } } : init);
  },
  /** 已完成的 3D 重建（含舊的、不是背景工作產生的結果） */
  cadJob: (jobId: string) => request<Schemas["CadJobSummary"]>(`/cad/jobs/${encodeURIComponent(jobId)}`),
  partReconstructions: (id: string) => request<Schemas["CadJobListResponse"]>(`/parts/${encodeURIComponent(id)}/reconstructions`),
  cadEvalRuns: () => request<Schemas["CadEvalRunsResponse"]>("/eval/cad-runs"),
};
