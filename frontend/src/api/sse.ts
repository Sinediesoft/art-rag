// SSE 解析：全專案只有這一份實作（共用層 §八）。問答（POST /chat）、3D 重建（POST /cad/reconstruct）、
// 庫存 Text-to-SQL（POST /inventory/ask）與生產排程（POST /schedule/solve）共用。
// 瀏覽器內建 EventSource 只支援 GET，所以用 fetch 讀取串流。事件格式見 shared/sse_events.md。
import {
  API_BASE,
  renewIfExpired,
  type Schemas,
  type AxisDay,
  type ChatRequest,
  type ConstraintScore,
  type InventoryAskRequest,
  type MemoryEvent,
  type PartSummary,
  type PlannedWorkOrder,
  type ReconstructRequest,
  type ScheduledOp,
  type ScheduleKpis,
  type ScheduleSolveRequest,
  type ScheduleWorkOrder,
} from "./client";

export interface SourceItem {
  ref: number;
  chunk_id: string;
  /** 機密等級：畫作「公開」，圖紙「內部」或「機密」（第 4 段只把公開段落送 Jev） */
  level?: string;
  /** 畫作段落才有 */
  artwork_id?: string;
  artwork_title?: string;
  /** 工廠圖紙段落與畫作的「色彩分析」段落（`<id>#color`）source_url 為 null，改顯示 source_label */
  part_id?: string;
  source_label?: string;
  title: string;
  topic: string;
  text: string;
  source_url: string | null;
  license: string;
  score: number;
}

export interface SourcesEvent {
  request_id: string;
  artwork_id: string | null;
  part_id?: string | null;
  strategy: string;
  use_retrieval: boolean;
  sources: SourceItem[];
  /** 檢索段落篩選（MIRA 的 Rearrange）；沒有篩選時為 null。fallback 有值代表篩選失敗、用原本的段落 */
  rearrange?: { candidates: number; kept: number; ms: number; fallback: string | null } | null;
  /** 第 3 段：Metadata Filter（依目前身分的資料範圍產生） */
  filter?: MetaFilterInfo;
  /** 第 3 段檢索出的候選段落數（第 4 段過濾前） */
  candidates?: number;
  /** 第 4～6 段：雙重驗證、評分重排、生成閘門（docs/adr/015）；關檢索時為 null */
  post_filter?: PostFilterInfo | null;
}

export type MetaFilterInfo = Schemas["MetaFilterInfo"];
export type GuardCheck = Schemas["GuardCheck"];
export type JevCallInfo = Schemas["JevCallInfo"];

/** 第 4～6 段裡的一段：誰判斷的、逐段的結果、送給 Jev 的內容（只有公開段落才會送） */
export interface StageInfo {
  engine: "jev" | "local";
  checks: GuardCheck[];
  call: JevCallInfo | null;
  fallback_reason: string | null;
  ms: number;
}

export interface GateInfo extends StageInfo {
  /** false＝降級回應「查無資料」，不呼叫 LLM */
  passed: boolean;
  message: string | null;
}

export interface PostFilterInfo {
  /** jev／local：智慧助理的第 4～6 段；scan：其他頁面，只用地端規則剔除有洩密風險的段落 */
  mode: "jev" | "local" | "scan";
  engine: "jev" | "local";
  candidates: number;
  kept: number;
  /** 第 4 段 security_leak_check 剔除的段落；by＝誰抓到的（Jev 或地端規則） */
  flagged: { chunk_id: string; title: string; topic: string; by?: "Jev" | "地端" }[];
  /** 與提問無關、分數太低或超過 3 段上限，沒放進上下文的段落 */
  dropped: { chunk_id: string; title: string; topic: string }[];
  /** 送 Jev 的段落數（只有公開段落）／留在地端判斷的段落數 */
  cloud: number;
  local: number;
  /** 第 4 段 Jev Noul 雙重驗證（is_relevant／security_leak_check） */
  verify: StageInfo;
  /** 第 5 段 Jev Score 評分重排（scan 模式沒有） */
  rerank: StageInfo | null;
  /** 第 6 段生成閘門（scan 模式沒有） */
  gate: GateInfo | null;
  /** 不送 Jev 的段落由本地 Qwen3-VL 判斷 is_relevant（段落篩選開著才有；candidates ≤ 1 時沒有呼叫模型） */
  rearrange?: SourcesEvent["rearrange"];
  /** 第 4～6 段送 Jev 的位元組數合計 */
  egress_bytes: number;
  ms: number;
}

export interface DoneEvent {
  request_id: string;
  strategy_requested: string;
  strategy_used: string;
  model: string;
  fallback: boolean;
  fallback_reason: string | null;
  prompt_version: string;
  use_retrieval: boolean;
  latency_ms: { retrieval: number; first_token: number | null; generation: number; total: number };
  tokens: { input: number; output: number };
  cost_twd: number;
  /** 送出本機的資料量；本地策略只有第 4～6 段送 Jev 的代號化公開段落（jev_bytes） */
  egress: { images: number; chunks: number; bytes: number; jev_bytes?: number };
  /** 第 6 段生成閘門沒過：回的是「查無資料」，沒有呼叫 LLM */
  degraded?: boolean;
}

export interface ErrorEvent {
  code: string;
  message: string;
  request_id: string;
}

type Handlers = Record<string, ((data: any) => void) | undefined> & {
  error?: (e: ErrorEvent) => void;
};

/** POST 一個 JSON body，依事件名稱分派 SSE 事件 */
export async function streamSSE(path: string, body: unknown, handlers: Handlers, signal?: AbortSignal) {
  let res: Response;
  const send = () =>
    fetch(`${API_BASE}/api/v1${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
  try {
    res = await send();
    // 憑證沒有或過期：重新取得（預設訪客）再送一次（client.ts 的 renewIfExpired）
    if (await renewIfExpired(res)) res = await send();
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    handlers.error?.({ code: "NETWORK_ERROR", message: "連不上伺服器", request_id: "" });
    return;
  }
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => null);
    handlers.error?.({
      code: data?.error?.code ?? `HTTP_${res.status}`,
      message: data?.error?.message ?? res.statusText,
      request_id: data?.error?.request_id ?? res.headers.get("X-Request-ID") ?? "",
    });
    return;
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  const dispatch = (block: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length) handlers[event]?.(JSON.parse(data.join("\n")));
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value.replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        dispatch(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 2);
      }
    }
    if (buffer.trim()) dispatch(buffer);
  } catch (err) {
    if ((err as Error).name !== "AbortError") {
      handlers.error?.({ code: "STREAM_ERROR", message: "串流中斷", request_id: "" });
    }
  }
}

export interface ChatHandlers {
  onSources?: (e: SourcesEvent) => void;
  onToken?: (text: string) => void;
  onDone?: (e: DoneEvent) => void;
  onError?: (e: ErrorEvent) => void;
}

export function streamChat(body: ChatRequest, h: ChatHandlers, signal?: AbortSignal) {
  return streamSSE(
    "/chat",
    body,
    {
      sources: h.onSources,
      token: (d) => h.onToken?.(d.text),
      done: h.onDone,
      error: h.onError,
    },
    signal,
  );
}

// ---- 3D 重建（POST /cad/reconstruct）
export type Dims = { width: number; depth: number; height: number };

export interface CadMetaEvent {
  request_id: string;
  job_id: string;
  strategy: string;
  model: string;
  image_id: string | null;
  part: PartSummary | null;
  identified: { matched: boolean; best_part_id: string | null } | null;
  /** kb＝知識庫圖紙、rectified＝照片已依知識庫圖紙拉正、upload＝未收錄的圖紙 */
  layout: "kb" | "rectified" | "upload";
  input_url: string;
  input_size: [number, number];
  scale_to: Dims | null;
  scale_source: string | null;
}

export interface CadResultEvent {
  ok: boolean;
  error: string | null;
  code: string;
  valid: boolean | null;
  /** 實體原本有瑕疵、已用 ShapeFix 自動修復 */
  repaired: boolean | null;
  raw_dims: Dims | null;
  dims: Dims | null;
  scale: number | null;
  volume: number | null;
  faces: number | null;
  /** 與知識庫標準模型的 IoU（對齊後）；未收錄圖紙為 null */
  iou: number | null;
  /** 外框對齊到圖紙標註尺寸後的 IoU */
  iou_bbox: number | null;
  files: Record<string, string>;
}

export interface CadDoneEvent {
  request_id: string;
  job_id: string;
  strategy: string;
  model: string;
  latency_ms: { first_token: number | null; generation: number; exec: number | null; total: number };
  tokens: { input: number; output: number };
  egress: { images: number; chunks: number; bytes: number };
}

export interface CadHandlers {
  onMeta?: (e: CadMetaEvent) => void;
  onToken?: (text: string) => void;
  onExecuting?: () => void;
  onResult?: (e: CadResultEvent) => void;
  onDone?: (e: CadDoneEvent) => void;
  onError?: (e: ErrorEvent) => void;
}

export function streamReconstruct(body: ReconstructRequest, h: CadHandlers, signal?: AbortSignal) {
  return streamSSE(
    "/cad/reconstruct",
    body,
    {
      meta: h.onMeta,
      token: (d) => h.onToken?.(d.text),
      executing: () => h.onExecuting?.(),
      result: h.onResult,
      done: h.onDone,
      error: h.onError,
    },
    signal,
  );
}

// ---- 工廠庫存 Text-to-SQL（POST /inventory/ask）
export interface SqlAttemptEvent {
  attempt: number;
  sql: string;
  ok: boolean;
  /** 靜態檢查或執行失敗的訊息；會回饋給模型修正 */
  error: string | null;
}

export interface SqlResultEvent {
  columns: string[];
  rows: (string | number | null)[][];
  row_count: number;
  truncated: boolean;
  exec_ms: number;
}

export interface SqlDoneEvent {
  request_id: string;
  strategy_requested: string;
  strategy_used: string;
  model: string;
  fallback: boolean;
  fallback_reason: string | null;
  prompt_version: string;
  answer_prompt_version: string;
  attempts: number;
  latency_ms: { first_token: number | null; sql: number; exec: number; answer: number; total: number };
  tokens: { input: number; output: number };
  egress: { images: number; chunks: number; bytes: number };
}

export interface SqlHandlers {
  onMeta?: (e: { request_id: string; as_of: string; prompt_version: string }) => void;
  onAttempt?: (e: { n: number; previous_error: string | null }) => void;
  onSqlToken?: (text: string) => void;
  onSql?: (e: SqlAttemptEvent) => void;
  onResult?: (e: SqlResultEvent) => void;
  onToken?: (text: string) => void;
  onDone?: (e: SqlDoneEvent) => void;
  onError?: (e: ErrorEvent) => void;
}

export function streamInventoryAsk(body: InventoryAskRequest, h: SqlHandlers, signal?: AbortSignal) {
  return streamSSE(
    "/inventory/ask",
    body,
    {
      meta: h.onMeta,
      attempt: h.onAttempt,
      sql_token: (d) => h.onSqlToken?.(d.text),
      sql: h.onSql,
      result: h.onResult,
      token: (d) => h.onToken?.(d.text),
      done: h.onDone,
      error: h.onError,
    },
    signal,
  );
}

// ---- 生產排程（POST /schedule/solve）
export interface ScheduleMetaEvent {
  request_id: string;
  engine: "timefold" | "greedy";
  engine_label: string;
  engine_version: string | null;
  /** Timefold 連不上而改用簡易排程的原因；null＝照指定的引擎 */
  fallback_reason: string | null;
  seconds: number;
  /** 連續這麼多秒沒找到更好的解就提前結束 */
  unimproved_seconds: number;
  problem: {
    plan_start: string;
    day_minutes: number;
    n_work_orders: number;
    n_operations: number;
    n_machines: number;
    n_pinned: number;
    total_work_min: number;
    skipped: { wo_no: string; part_id: string; reason: string }[];
  };
  work_orders: ScheduleWorkOrder[];
  machines: { machine_id: string; name: string; machine_type: string; site: string }[];
  axis: AxisDay[];
}

export interface ScoreFields {
  score: string;
  hard: number;
  medium: number;
  soft: number;
  structural?: number;
  feasible: boolean;
}

export interface ScheduleProgressEvent extends ScoreFields {
  elapsed_ms: number;
  /** 建構初始解／局部搜尋最佳化／交期優先派工 */
  phase: string;
  initial_score: string | null;
  improvements: number;
  operations: ScheduledOp[];
  kpis: ScheduleKpis;
}

export interface ScheduleSolutionEvent extends ScoreFields {
  run_id: string;
  engine: string;
  status: "done" | "stopped";
  initial_score: string | null;
  /** 後端依同一套規則重算的總分是否與 Timefold 一致；簡易排程為 null */
  score_check: boolean | null;
  analysis: ConstraintScore[];
  operations: ScheduledOp[];
  work_orders: PlannedWorkOrder[];
  kpis: ScheduleKpis;
  axis: AxisDay[];
}

export interface ScheduleDoneEvent {
  request_id: string;
  run_id: string;
  engine: string;
  status: string;
  score: string;
  initial_score: string | null;
  improvements: number;
  latency_ms: { build: number; solve: number; total: number };
  egress: { images: number; chunks: number; bytes: number };
  /** 這次排程期間記憶體管理釋放了哪些模型 */
  memory: MemoryEvent | null;
}

/** 沒有更好的解時每秒一次的心跳 */
export interface ScheduleTickEvent {
  elapsed_ms: number;
  phase: string;
  improvements: number;
}

export interface ScheduleHandlers {
  onMeta?: (e: ScheduleMetaEvent) => void;
  onProgress?: (e: ScheduleProgressEvent) => void;
  onTick?: (e: ScheduleTickEvent) => void;
  onSolution?: (e: ScheduleSolutionEvent) => void;
  onDone?: (e: ScheduleDoneEvent) => void;
  onError?: (e: ErrorEvent) => void;
}

export function streamScheduleSolve(body: ScheduleSolveRequest, h: ScheduleHandlers, signal?: AbortSignal) {
  return streamSSE(
    "/schedule/solve",
    body,
    {
      meta: h.onMeta,
      progress: h.onProgress,
      tick: h.onTick,
      solution: h.onSolution,
      done: h.onDone,
      error: h.onError,
    },
    signal,
  );
}
