import { useCallback, useEffect, useRef, useState } from "react";
import type { ScheduleSolveRequest } from "../api/client";
import {
  streamScheduleSolve,
  type ErrorEvent,
  type ScheduleDoneEvent,
  type ScheduleMetaEvent,
  type ScheduleProgressEvent,
  type ScheduleSolutionEvent,
} from "../api/sse";

export type SolveStatus = "idle" | "starting" | "solving" | "done" | "error";

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
};

export function useScheduleSolve(onFinished?: () => void) {
  const [state, setState] = useState<SolveState>(initial);
  const abortRef = useRef<AbortController | null>(null);
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const start = useCallback((body: ScheduleSolveRequest) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ ...initial, status: "starting" });
    void streamScheduleSolve(
      body,
      {
        onMeta: (meta) => setState((s) => ({ ...s, meta, status: "solving" })),
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
          setState((s) => ({ ...s, done, status: "done", elapsedMs: done.latency_ms.solve }));
          finishedRef.current?.();
        },
        onError: (error) => setState((s) => ({ ...s, error, status: "error" })),
      },
      ctrl.signal,
    );
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    setState(initial);
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start, reset };
}

/** "0hard/-3819medium/-32540soft" → 分數變化折線的一個點（時間 0） */
function parseScore(score: string) {
  const m = score.match(/(-?\d+)hard\/(-?\d+)medium\/(-?\d+)soft/);
  return { elapsed_ms: 0, medium: Number(m?.[2] ?? 0), soft: Number(m?.[3] ?? 0), phase: "建構初始解" };
}
