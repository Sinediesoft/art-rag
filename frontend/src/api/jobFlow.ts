// 背景工作的前端流程：建立工作（202）→ 訂閱事件（斷線用 Last-Event-ID 接上，連不上改輪詢）→ 取結果。
// 事件與結果對應回原本 SSE 入口的 handler（CadHandlers、ScheduleHandlers），所以畫面元件的狀態形狀不變，
// 另外多了「排隊中」「已取消」「工作編號」。離開頁面只停止訂閱，不取消工作。
import { api, ApiError, assetUrl, fetchText, withIdempotency } from "./client";
import { followJob } from "./jobs";
import type { CadDoneEvent, CadHandlers, CadMetaEvent, CadResultEvent, ErrorEvent, ScheduleDoneEvent, ScheduleHandlers, ScheduleMetaEvent, ScheduleProgressEvent, ScheduleSolutionEvent, ScheduleTickEvent } from "./sse";
import type { CadJobCreate, Job, ScheduleJobCreate } from "./types";

export interface FlowHooks {
  /** 工作編號（建立成功或接回既有工作時）：存起來才能取消、提前結束、重新整理後接回 */
  onJob?: (jobId: string) => void;
  /** 在佇列裡（還沒有 worker 領走，或 worker 當機後重新排隊） */
  onQueued?: (info: { requeued: boolean; delayed: boolean }) => void;
  /** worker 領走了，開始執行 */
  onRunning?: (info: { attempt: number }) => void;
  /** 已取消：沒有寫回任何結果 */
  onCancelled?: () => void;
  /** 事件串流中斷、正在重連 */
  onReconnect?: (n: number) => void;
}

export interface FlowOptions {
  /** 接回既有的工作，不建立新的 */
  attach?: string;
  /** 事件從這個 id 之後開始（預設 0：把全部事件重播一次，狀態就會回到目前的進度） */
  after?: number;
}

const failure = (e: unknown): ErrorEvent => (e instanceof ApiError ? { code: e.code, message: e.message, request_id: e.requestId } : { code: "ERROR", message: String((e as Error)?.message ?? e), request_id: "" });

/** 這個帳號有沒有同一個目標、排隊或執行中的 3D 重建（重新整理後再按一次，接回而不是重複建立） */
export async function findActiveCad(body: CadJobCreate): Promise<Job | null> {
  const { items } = await api.jobs({ kind: "cad", active: true });
  return (
    items.find((j) => {
      const t = (j.target ?? {}) as { part_id?: string | null; image_id?: string | null };
      return (t.part_id ?? null) === (body.part_id ?? null) && (t.image_id ?? null) === (body.image_id ?? null);
    }) ?? null
  );
}

export async function findActiveSchedule(): Promise<Job | null> {
  const { items } = await api.jobs({ kind: "schedule", active: true });
  return items[0] ?? null;
}

// ---------------------------------------------------------------- 3D 重建
export type CadFlowHandlers = CadHandlers & FlowHooks & { onCode?: (code: string) => void };

export async function runCadJob(body: CadJobCreate, h: CadFlowHandlers, signal: AbortSignal, opts: FlowOptions = {}): Promise<void> {
  let jobId = opts.attach;
  let delayed = false;
  try {
    if (!jobId) {
      const active = await findActiveCad(body).catch(() => null);
      if (active) jobId = active.id;
      else {
        // 同一件事的重送共用一個 Idempotency-Key：連按、網路重試只會有一個工作
        const created = await withIdempotency(`cad-job:${JSON.stringify(body)}`, (key) => api.createCadJob(body, key));
        jobId = created.job_id;
        delayed = created.delayed_dispatch;
      }
    }
  } catch (e) {
    if (!signal.aborted) h.onError?.(failure(e));
    return;
  }
  if (signal.aborted) return;
  h.onJob?.(jobId);
  let sawMeta = false;
  const job = await followJob(
    jobId,
    {
      onEvent: (e) => {
        if (e.type === "created") h.onQueued?.({ requeued: false, delayed });
        else if (e.type === "requeued") h.onQueued?.({ requeued: true, delayed: false });
        else if (e.type === "started") h.onRunning?.({ attempt: Number(e.data.attempt ?? 1) });
        else if (e.type === "meta") {
          sawMeta = true;
          h.onMeta?.(e.data as CadMetaEvent);
        } else if (e.type === "stage" && e.data.stage === "executing") h.onExecuting?.();
      },
      onReconnect: h.onReconnect,
      onError: (err) => !signal.aborted && h.onError?.(err),
    },
    signal,
    { after: opts.after },
  );
  if (!job || signal.aborted) return;
  const summary = job.result as unknown as { meta?: CadMetaEvent; result?: CadResultEvent; done?: CadDoneEvent } | null;
  if (job.status === "cancelled") return h.onCancelled?.();
  if (summary?.result) {
    // 成功，或沙箱執行失敗（還是有輸入圖與程式碼可以給授權的人看）
    if (!sawMeta && summary.meta) h.onMeta?.(summary.meta);
    const code = summary.result.files?.["code.py"];
    if (code) h.onCode?.(await fetchText(assetUrl(code)).catch(() => ""));
    h.onResult?.(summary.result);
    if (summary.done) h.onDone?.(summary.done);
    return;
  }
  h.onError?.({ code: job.error?.code ?? "JOB_FAILED", message: job.error?.message ?? "3D 重建失敗", request_id: "" });
}

// ---------------------------------------------------------------- 排程
export type ScheduleFlowHandlers = ScheduleHandlers & FlowHooks;

export async function runScheduleJob(body: ScheduleJobCreate, h: ScheduleFlowHandlers, signal: AbortSignal, opts: FlowOptions = {}): Promise<void> {
  let jobId = opts.attach;
  let delayed = false;
  try {
    if (!jobId) {
      const active = await findActiveSchedule().catch(() => null);
      if (active) jobId = active.id;
      else {
        const created = await withIdempotency("schedule-job", (key) => api.createScheduleJob(body, key));
        jobId = created.job_id;
        delayed = created.delayed_dispatch;
      }
    }
  } catch (e) {
    if (!signal.aborted) h.onError?.(failure(e));
    return;
  }
  if (signal.aborted) return;
  h.onJob?.(jobId);
  const job = await followJob(
    jobId,
    {
      onEvent: (e) => {
        if (e.type === "created") h.onQueued?.({ requeued: false, delayed });
        else if (e.type === "requeued") h.onQueued?.({ requeued: true, delayed: false });
        else if (e.type === "started") h.onRunning?.({ attempt: Number(e.data.attempt ?? 1) });
        else if (e.type === "meta") h.onMeta?.(e.data as unknown as ScheduleMetaEvent);
        else if (e.type === "progress") h.onProgress?.(e.data as unknown as ScheduleProgressEvent);
        else if (e.type === "tick") h.onTick?.(e.data as unknown as ScheduleTickEvent);
      },
      onReconnect: h.onReconnect,
      onError: (err) => !signal.aborted && h.onError?.(err),
    },
    signal,
    { after: opts.after },
  );
  if (!job || signal.aborted) return;
  if (job.status === "cancelled") return h.onCancelled?.();
  if (job.status !== "succeeded" || !job.result) {
    return h.onError?.({ code: job.error?.code ?? "JOB_FAILED", message: job.error?.message ?? "排程失敗", request_id: "" });
  }
  const r = job.result as Record<string, any>;
  try {
    const run = await api.scheduleRun(r.run_id);
    const solution: ScheduleSolutionEvent = {
      run_id: run.run_id,
      engine: run.engine,
      status: (r.run_status ?? run.status) as "done" | "stopped",
      score: run.score ?? r.score,
      hard: run.hard ?? r.hard,
      medium: run.medium ?? r.medium,
      soft: run.soft ?? r.soft,
      feasible: (run.hard ?? r.hard) >= 0,
      initial_score: run.initial_score ?? null,
      score_check: run.score_check ?? null,
      analysis: run.analysis,
      operations: run.operations,
      work_orders: run.work_orders,
      kpis: run.kpis,
      axis: run.axis,
    };
    h.onSolution?.(solution);
    const done: ScheduleDoneEvent = {
      request_id: job.id,
      run_id: r.run_id,
      engine: r.engine,
      status: r.run_status ?? run.status,
      score: r.score,
      initial_score: r.initial_score ?? null,
      improvements: r.improvements ?? 0,
      latency_ms: r.latency_ms ?? { build: 0, solve: run.solve_ms ?? 0, total: run.solve_ms ?? 0 },
      egress: r.egress ?? { images: 0, chunks: 0, bytes: 0 },
      memory: null,
    };
    h.onDone?.(done);
  } catch (e) {
    h.onError?.(failure(e));
  }
}
