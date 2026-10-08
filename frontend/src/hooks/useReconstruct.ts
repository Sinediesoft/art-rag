import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ReconstructRequest } from "../api/client";
import {
  streamReconstruct,
  type CadDoneEvent,
  type CadMetaEvent,
  type CadResultEvent,
  type ErrorEvent,
} from "../api/sse";

/** preparing＝前處理、辨識、讀尺寸；generating＝模型串流程式碼；executing＝沙箱執行 */
export type CadStatus = "idle" | "preparing" | "generating" | "executing" | "done" | "error";

export interface CadState {
  status: CadStatus;
  meta: CadMetaEvent | null;
  code: string;
  result: CadResultEvent | null;
  done: CadDoneEvent | null;
  error: ErrorEvent | null;
  startedAt: number;
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
  cached: false,
};

export function useReconstruct() {
  const [state, setState] = useState<CadState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback((body: ReconstructRequest) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ ...initial, status: "preparing", startedAt: Date.now() });
    void streamReconstruct(
      body,
      {
        onMeta: (meta) => setState((s) => ({ ...s, meta, status: "generating" })),
        onToken: (t) => setState((s) => ({ ...s, code: s.code + t })),
        onExecuting: () => setState((s) => ({ ...s, status: "executing" })),
        onResult: (result) => setState((s) => ({ ...s, result, code: result.code || s.code })),
        onDone: (done) => setState((s) => ({ ...s, done, status: "done" })),
        onError: (error) => setState((s) => ({ ...s, error, status: "error" })),
      },
      ctrl.signal,
    );
  }, []);

  const load = useCallback(async (jobId: string) => {
    abortRef.current?.abort();
    setState({ ...initial, status: "preparing", startedAt: Date.now() });
    try {
      const job = await api.cadJob(jobId);
      const result = job.result as unknown as CadResultEvent;
      setState({
        ...initial,
        status: "done",
        meta: job.meta as unknown as CadMetaEvent,
        result,
        done: job.done as unknown as CadDoneEvent,
        code: result.code,
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

  const abort = useCallback(() => abortRef.current?.abort(), []);
  // 元件卸載（離開頁面、切換身分時功能頁重建）就中止串流：舊身分的結果不再接收
  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start, load, abort };
}
