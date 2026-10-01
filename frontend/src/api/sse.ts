// SSE 解析：全專案只有這一份實作（共用層 §八）。問答（POST /chat）與 3D 重建（POST /cad/reconstruct）共用。
// 瀏覽器內建 EventSource 只支援 GET，所以用 fetch 讀取串流。事件格式見 shared/sse_events.md。
import { API_BASE, type ChatRequest, type PartSummary, type ReconstructRequest } from "./client";

export interface SourceItem {
  ref: number;
  chunk_id: string;
  /** 畫作段落才有 */
  artwork_id?: string;
  artwork_title?: string;
  /** 工廠圖紙段落才有；source_url 為 null，改顯示 source_label（內部文件名稱） */
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
  /** 送出本機的資料量；本地策略恆為 0 */
  egress: { images: number; chunks: number; bytes: number };
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
  try {
    res = await fetch(`${API_BASE}/api/v1${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
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
