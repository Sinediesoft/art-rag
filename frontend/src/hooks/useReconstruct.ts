import { useCallback, useEffect, useRef, useState } from "react";
import { api, assetUrl, fetchText, type CadJobCreate, type ReconstructRequest } from "../api/client";
import { runCadJob } from "../api/jobFlow";
import type { CadDoneEvent, CadMetaEvent, CadResultEvent, ErrorEvent } from "../api/sse";

/**
 * queued＝在佇列等 worker；preparing＝前處理、辨識、讀尺寸；generating＝模型產生程式碼；executing＝沙箱執行；
 * cancelled＝已取消（沒有結果）；stopped＝這個畫面停止追蹤（工作仍在背景執行）
 */
export type CadStatus = "idle" | "queued" | "preparing" | "generating" | "executing" | "done" | "error" | "cancelled" | "stopped";

export interface CadState {
  status: CadStatus;
  meta: CadMetaEvent | null;
  /** 程式碼只放在受保護的 code.py 產物，工作完成後才取回（事件裡沒有模型輸出） */
  code: string;
  result: CadResultEvent | null;
  done: CadDoneEvent | null;
  error: ErrorEvent | null;
  startedAt: number;
  /** 背景工作編號：取消、重新整理後接回用 */
  jobId: string | null;
  /** 事件串流斷線、正在重連（輪詢備援期間也算） */
  reconnecting: number;
  /** 從已完成的工作載入（不是現場重跑） */
  cached: boolean;
}

const initial: CadState = {
  status: "idle",
  meta: null,
  code: "",
  result: null,
  done: null,
  error: null,
  startedAt: 0,
  jobId: null,
  reconnecting: 0,
  cached: false,
};

export function useReconstruct() {
  const [state, setState] = useState<CadState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const follow = useCallback((body: CadJobCreate, attach?: string) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ ...initial, status: "queued", startedAt: Date.now() });
    void runCadJob(
      body,
      {
        onJob: (jobId) => setState((s) => ({ ...s, jobId })),
        onQueued: () => setState((s) => ({ ...s, status: "queued" })),
        onRunning: () => setState((s) => ({ ...s, status: s.status === "queued" ? "preparing" : s.status })),
        onReconnect: (n) => setState((s) => ({ ...s, reconnecting: n })),
        onMeta: (meta) => setState((s) => ({ ...s, meta, status: "generating", reconnecting: 0 })),
        onExecuting: () => setState((s) => ({ ...s, status: "executing" })),
        onCode: (code) => setState((s) => ({ ...s, code })),
        onResult: (result) => setState((s) => ({ ...s, result })),
        onDone: (done) => setState((s) => ({ ...s, done, status: "done" })),
        onError: (error) => setState((s) => ({ ...s, error, status: "error" })),
        onCancelled: () => setState((s) => ({ ...s, status: "cancelled" })),
      },
      ctrl.signal,
      { attach },
    );
  }, []);

  const start = useCallback((body: ReconstructRequest) => follow({ part_id: body.part_id, image_id: body.image_id, strategy: body.strategy }), [follow]);

  /** 接回一個已經存在的工作（重新整理後、或從工作清單進來）：事件從頭重播，狀態回到目前進度 */
  const attach = useCallback((jobId: string) => follow({}, jobId), [follow]);

  /** 從已完成的結果載入（舊的結果也讀得到：GET /cad/jobs/{id}） */
  const load = useCallback(async (jobId: string) => {
    abortRef.current?.abort();
    setState({ ...initial, status: "queued", startedAt: Date.now() });
    try {
      const job = await api.cadJob(jobId);
      const result = job.result as unknown as CadResultEvent;
      let code = result.code ?? "";
      const url = result.files?.["code.py"];
      if (!code && url) code = await fetchText(assetUrl(url)).catch(() => "");
      setState({
        ...initial,
        status: "done",
        jobId,
        meta: job.meta as unknown as CadMetaEvent,
        result,
        done: job.done as unknown as CadDoneEvent,
        code,
        cached: true,
      });
    } catch (e) {
      setState({
        ...initial,
        status: "error",
        error: { code: "CAD_JOB_NOT_FOUND", message: (e as Error).message, request_id: "" },
      });
    }
  }, []);

  /** 取消背景工作（POST /jobs/{id}/cancel）：worker 停止、不發布任何產物；畫面等「已取消」事件 */
  const cancel = useCallback(async () => {
    const id = state.jobId;
    if (!id) return abortRef.current?.abort();
    try {
      await api.cancelJob(id);
    } catch (e) {
      setState((s) => ({ ...s, error: { code: (e as { code?: string }).code ?? "ERROR", message: (e as Error).message, request_id: "" } }));
    }
  }, [state.jobId]);

  // 元件卸載（離開頁面、切換身分時功能頁重建）只停止訂閱：工作照常在 worker 執行，舊身分的結果不再接收
  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start, load, attach, cancel };
}
