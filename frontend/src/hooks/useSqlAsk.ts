import { useCallback, useEffect, useRef, useState } from "react";
import type { InventoryAskRequest } from "../api/client";
import {
  streamInventoryAsk,
  type ErrorEvent,
  type SqlAttemptEvent,
  type SqlDoneEvent,
  type SqlResultEvent,
} from "../api/sse";

/** generating：模型寫 SQL 中；executing：SQL 寫完、檢查與執行中；answering：依結果回答中 */
export type SqlStatus = "idle" | "generating" | "executing" | "answering" | "done" | "error";

export interface SqlAskState {
  status: SqlStatus;
  attempt: number;
  /** 目前這次嘗試串流中的 SQL（含模型輸出的 ``` 標記，顯示前會去掉） */
  draft: string;
  attempts: SqlAttemptEvent[];
  result: SqlResultEvent | null;
  answer: string;
  done: SqlDoneEvent | null;
  error: ErrorEvent | null;
}

const initial: SqlAskState = {
  status: "idle",
  attempt: 0,
  draft: "",
  attempts: [],
  result: null,
  answer: "",
  done: null,
  error: null,
};

export function useSqlAsk() {
  const [state, setState] = useState<SqlAskState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback((body: InventoryAskRequest) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setState({ ...initial, status: "generating" });
    void streamInventoryAsk(
      body,
      {
        onAttempt: (e) => setState((s) => ({ ...s, attempt: e.n, draft: "", status: "generating" })),
        onSqlToken: (t) => setState((s) => ({ ...s, draft: s.draft + t })),
        onSql: (e) =>
          setState((s) => ({ ...s, attempts: [...s.attempts, e], status: e.ok ? "answering" : "generating" })),
        onResult: (result) => setState((s) => ({ ...s, result, status: "answering" })),
        onToken: (t) => setState((s) => ({ ...s, answer: s.answer + t })),
        onDone: (done) => setState((s) => ({ ...s, done, status: "done" })),
        onError: (error) => setState((s) => ({ ...s, error, status: "error" })),
      },
      ctrl.signal,
    );
  }, []);

  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start };
}

/** 串流中的模型輸出去掉 ```sql 標記，只留 SQL */
export const cleanSqlDraft = (text: string) => text.replace(/```(?:sql|sqlite)?\n?/gi, "").trim();
