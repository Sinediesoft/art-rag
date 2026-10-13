// 背景工作（3D 重建、排程）：狀態、取消、產物與事件追蹤。
// 追蹤只是「訂閱」：離開頁面、切換身分、元件卸載都只停止訂閱，不會取消工作；
// 回到頁面可以用 listJobs／overview.active_job 找回，用 Last-Event-ID 從斷掉的地方接上。
import { ApiError, json, request } from "./http";
import { JOB_TERMINAL, streamJobEvents, type JobEventMsg } from "./sse";
import type { Job, Schemas } from "./types";

export const jobsApi = {
  job: (jobId: string) => request<Job>(`/jobs/${encodeURIComponent(jobId)}`),
  /** 目前身分看得到的工作（最新的在前）；active＝只列排隊或執行中的 */
  jobs: (q: { kind?: "cad" | "schedule"; active?: boolean; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.kind) p.set("kind", q.kind);
    if (q.active) p.set("active", "true");
    if (q.limit) p.set("limit", String(q.limit));
    const qs = p.toString();
    return request<Schemas["JobListResponse"]>(`/jobs${qs ? `?${qs}` : ""}`);
  },
  /** 取消：排隊中直接取消；執行中請 worker 停止，取消後不會寫回任何結果 */
  cancelJob: (jobId: string) => request<Schemas["JobCancelResponse"]>(`/jobs/${encodeURIComponent(jobId)}/cancel`, json({})),
};

// ---------------------------------------------------------------- 事件追蹤（訂閱）
export interface FollowHandlers {
  onEvent?: (e: JobEventMsg) => void;
  /** 工作到終態後讀回的完整內容（結果、錯誤、產物清單）；輪詢備援也由這裡收尾 */
  onJob?: (job: Job) => void;
  /** 輪詢備援期間每次讀到的狀態（事件串流連不上時） */
  onPoll?: (job: Job) => void;
  /** 不能再追蹤：沒有權限、工作不存在或已過期（不重試） */
  onError?: (e: { code: string; message: string; request_id: string }) => void;
  /** 事件串流中斷，準備第 n 次重連 */
  onReconnect?: (n: number) => void;
}

export interface FollowOptions {
  /** 從這個事件 id 之後開始（重新整理後接回時從 0，會把全部事件重播一次） */
  after?: number;
  retryMs?: number[];
  pollMs?: number;
  maxStreamFailures?: number;
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(done, ms);
    function done() {
      clearTimeout(t);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });

const FATAL = new Set([401, 403, 404]);
const asFailure = (e: unknown) => (e instanceof ApiError ? { code: e.code, message: e.message, request_id: e.requestId } : { code: "ERROR", message: String((e as Error)?.message ?? e), request_id: "" });

/**
 * 追蹤一個工作直到終態。事件串流中斷就用 Last-Event-ID 從斷掉的地方重連（事件有持久化的遞增 id，不會重複套用）；
 * 連續失敗幾次後改用輪詢（GET /jobs/{id}）當備援；沒有權限或工作不存在就停止並回報。
 * 這只是「訂閱」：signal 中止（離開頁面、切換身分）只停止追蹤，工作照常在 worker 執行。
 * 回傳終態時的工作；中止或不能追蹤時回 null。
 */
export async function followJob(jobId: string, h: FollowHandlers, signal: AbortSignal, opts: FollowOptions = {}): Promise<Job | null> {
  const retry = opts.retryMs ?? [300, 800, 2000, 4000];
  const pollMs = opts.pollMs ?? 1500;
  const maxFailures = opts.maxStreamFailures ?? 3;
  let last = opts.after ?? 0;
  let failures = 0;
  let terminal = false;
  let reconnects = 0;
  while (!signal.aborted && !terminal) {
    try {
      await streamJobEvents(
        jobId,
        last,
        (e) => {
          last = Math.max(last, e.id);
          failures = 0;
          if (JOB_TERMINAL.has(e.type)) terminal = true;
          h.onEvent?.(e);
        },
        signal,
      );
    } catch (e) {
      if (signal.aborted) return null;
      if (e instanceof ApiError && FATAL.has(e.status)) {
        h.onError?.(asFailure(e));
        return null;
      }
      failures++;
    }
    if (signal.aborted) return null;
    if (terminal) break;
    if (failures >= maxFailures) {
      // 輪詢備援：事件串流一直連不上時，仍然拿得到狀態與終態
      while (!signal.aborted) {
        try {
          const job = await jobsApi.job(jobId);
          h.onPoll?.(job);
          if (job.status !== "queued" && job.status !== "running") {
            terminal = true;
            break;
          }
        } catch (e) {
          if (e instanceof ApiError && FATAL.has(e.status)) {
            h.onError?.(asFailure(e));
            return null;
          }
        }
        await sleep(pollMs, signal);
      }
      break;
    }
    h.onReconnect?.(++reconnects);
    await sleep(retry[Math.min(failures, retry.length - 1)], signal);
  }
  if (signal.aborted) return null;
  try {
    const job = await jobsApi.job(jobId);
    h.onJob?.(job);
    return job;
  } catch (e) {
    h.onError?.(asFailure(e));
    return null;
  }
}
