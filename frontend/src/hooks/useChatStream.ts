import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatRequest } from "../api/client";
import { streamChat, type DoneEvent, type ErrorEvent, type SourcesEvent } from "../api/sse";
import { withChatSettings, type Applied } from "../shell/settings";

export type ChatStatus = "idle" | "retrieving" | "streaming" | "done" | "error";

export interface ChatState {
  status: ChatStatus;
  sources: SourcesEvent | null;
  text: string;
  done: DoneEvent | null;
  error: ErrorEvent | null;
  /** 這一題套用的設定面板開關（shell/settings.ts）；請求已指定的欄位不套用 */
  applied: Applied;
}

const initial: ChatState = { status: "idle", sources: null, text: "", done: null, error: null, applied: {} };

export function useChatStream() {
  const [state, setState] = useState<ChatState>(initial);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback((body: ChatRequest) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const { body: sent, applied } = withChatSettings(body);
    setState({ ...initial, status: "retrieving", applied });
    void streamChat(
      sent,
      {
        onSources: (sources) => setState((s) => ({ ...s, sources, status: "streaming" })),
        onToken: (t) => setState((s) => ({ ...s, text: s.text + t, status: "streaming" })),
        onDone: (done) => setState((s) => ({ ...s, done, status: "done" })),
        onError: (error) => setState((s) => ({ ...s, error, status: "error" })),
      },
      ctrl.signal,
    );
  }, []);

  const abort = useCallback(() => abortRef.current?.abort(), []);
  // 元件卸載（離開頁面、切換身分時功能頁重建）就中止串流：舊身分的結果不再接收
  useEffect(() => () => abortRef.current?.abort(), []);
  return { state, start, abort };
}
