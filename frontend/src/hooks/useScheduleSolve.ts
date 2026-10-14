import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ScheduleSolveRequest } from "../api/client";
import { runScheduleJob } from "../api/jobFlow";
import type { ErrorEvent, ScheduleDoneEvent, ScheduleMetaEvent, ScheduleProgressEvent, ScheduleSolutionEvent } from "../api/sse";

/**
 * queued＝在佇列等 worker；cancelled＝已取消、沒有寫回；
 * stopped＝這個畫面停止追蹤（排程仍在背景執行，可以接回）
 */
export type SolveStatus = "idle" | "queued" | "starting" | "solving" | "done" | "error" | "cancelled" | "stopped";

export interface SolveState {
  status: SolveStatus;
  meta: ScheduleMetaEvent | null;
  /** 最新的最佳解（Timefold 每找到更好的解就更新一次） */
  progress: ScheduleProgressEvent | null;
  /** 求解經過的時間（心跳也會更新） */
  elapsedMs: number;
  /** 分數變化（畫成折線） */
  history: { elapsed_ms: number; medium: number; soft: number; phase: string }[];
  solution: ScheduleSolutionEvent | null;
  done: ScheduleDoneEvent | null;
  error: ErrorEvent | null;
  jobId: string | null;
  /** 已要求提前結束（採用目前最佳解並寫回），等 worker 收尾 */
  stopping: boolean;
  reconnecting: number;
}

const initial: SolveState = {
  status: "idle",
  meta: null,
  progress: null,
  elapsedMs: 0,
  history: [],
  solution: null,
  done: null,
  error: null,
  jobId: null,
  stopping: false,
  reconnecting: 0,
};

export function useScheduleSolve(onFinished?: () => void) {
  const [state, setState] = useState<SolveState>(initial);
  const abortRef = useRef<AbortController | null>(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const follow = useCallback((body: ScheduleSolveRequest, attach?: string) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ ...initial, status: "queued" });
    void runScheduleJob(
      { seconds: body.seconds, engine: body.engine },
      {
        onJob: (jobId) => setState((s) => ({ ...s, jobId })),
        onQueued: () => setState((s) => ({ ...s, status: "queued" })),
        onRunning: () => setState((s) => ({ ...s, status: s.status === "queued" ? "starting" : s.status })),
        onReconnect: (n) => setState((s) => ({ ...s, reconnecting: n })),
        onMeta: (meta) => setState((s) => ({ ...s, meta, status: "solving", reconnecting: 0 })),
        onProgress: (p) =>
          setState((s) => ({
            ...s,
            progress: p,
            elapsedMs: p.elapsed_ms,
            history: [
              // 第一筆前面補上建構出來的初始解，折線才看得出局部搜尋改善了多少
              ...(s.history.length === 0 && p.initial_score ? [parseScore(p.initial_score)] : []),
              ...s.history,
              { elapsed_ms: p.elapsed_ms, medium: p.medium, soft: p.soft, phase: p.phase },
            ],
          })),
        onTick: (t) => setState((s) => ({ ...s, elapsedMs: t.elapsed_ms })),
        onSolution: (solution) => setState((s) => ({ ...s, solution })),
        onDone: (done) => {
          setState((s) => ({ ...s, done, status: "done", elapsedMs: done.latency_ms.solve, stopping: false }));
          finishedRef.current?.();
        },
        onError: (error) => setState((s) => ({ ...s, error, status: "error", stopping: false })),
        onCancelled: () => {
          setState((s) => ({ ...s, status: "cancelled", stopping: false }));
          finishedRef.current?.();
        },
      },
      ctrl.signal,
      { attach },
    );
  }, []);

  const start = useCallback((body: ScheduleSolveRequest) => follow(body), [follow]);
  /** 接回排程範圍裡排隊或執行中的工作（重新整理、回到頁面）：事件從頭重播，狀態回到目前進度 */
  const attach = useCallback((jobId: string) => follow({}, jobId), [follow]);

  /** 提前結束：採用目前最佳的合法解並照常驗證、寫回（不是取消） */
  const stop = useCallback(async () => {
    setState((s) => ({ ...s, stopping: true }));
    try {
      await api.stopSchedule(state.jobId ?? undefined);
    } catch (e) {
      setState((s) => ({ ...s, stopping: false, error: { code: (e as { code?: string }).code ?? "ERROR", message: (e as Error).message, request_id: "" } }));
    }
  }, [state.jobId]);

  /** 取消：放棄這次求解、不寫回任何結果 */
  const cancel = useCallback(async () => {
    if (!state.jobId) return abortRef.current?.abort();
    try {
      await api.cancelJob(state.jobId);
    } catch (e) {
      setState((s) => ({ ...s, error: { code: (e as { code?: string }).code ?? "ERROR", message: (e as Error).message, request_id: "" } }));
    }
  }, [state.jobId]);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setState(initial);
  }, []);

  // 卸載只停止訂閱，不取消排程：回到頁面時 overview.active_job 讓它接回來
  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start, attach, stop, cancel, reset };
}

/** "0hard/-3819medium/-32540soft" → 分數變化折線的一個點（時間 0） */
function parseScore(score: string) {
  const m = score.match(/(-?\d+)hard\/(-?\d+)medium\/(-?\d+)soft/);
  return { elapsed_ms: 0, medium: Number(m?.[2] ?? 0), soft: Number(m?.[3] ?? 0), phase: "建構初始解" };
}
