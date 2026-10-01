import { useEffect, useRef, useState } from "react";
import type { ChatRequest } from "../api/client";
import { useChatStream } from "../hooks/useChatStream";
import { seconds, STRATEGY_LABEL, twd } from "../lib/format";
import { AnswerText } from "./common/CitationTag";
import { ErrorMessage, FeedbackButtons } from "./common/Feedback";
import { EgressBadge, FallbackBadge, NotInKbNotice } from "./common/StatusNotices";

/** 一次問答：開始串流、顯示引用、來源、延遲與成本。問答頁與策略比較頁共用。 */
export function ChatAnswer({
  request,
  compact = false,
  showSources = true,
}: {
  request: ChatRequest;
  compact?: boolean;
  showSources?: boolean;
}) {
  const { state, start } = useChatStream();
  const [activeRef, setActiveRef] = useState<number | null>(null);
  const [open, setOpen] = useState(!compact);
  const sourceRefs = useRef<Record<number, HTMLLIElement | null>>({});
  const key = JSON.stringify(request);

  useEffect(() => {
    start(request);
  }, [key, start]);

  const cite = (n: number) => {
    setActiveRef(n);
    setOpen(true);
    requestAnimationFrame(() =>
      sourceRefs.current[n]?.scrollIntoView({ behavior: "smooth", block: "nearest" }),
    );
  };

  const { status, text, done, error, sources } = state;

  if (error?.code === "NOT_IN_KB") return <NotInKbNotice />;

  return (
    <div className="flex flex-col gap-3">
      {status === "retrieving" && (
        <p className="flex items-center gap-2 text-sm text-ink-soft">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-seal" />
          檢索知識庫中…
        </p>
      )}
      {status === "streaming" && !text && (
        <p className="flex items-center gap-2 text-sm text-ink-soft">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-seal" />
          已取回 {sources?.sources.length ?? 0} 段資料，等待模型回答…
        </p>
      )}
      {text && (
        <div
          className={`whitespace-pre-wrap leading-relaxed ${compact ? "text-[15px]" : "text-base"} ${
            status === "streaming" ? "caret" : ""
          }`}
        >
          <AnswerText text={text.trim()} activeRef={activeRef} onCite={cite} />
        </div>
      )}
      {error && (
        <ErrorMessage
          title={
            error.code === "STRATEGY_UNAVAILABLE"
              ? "這個生成端目前無法使用"
              : error.code === "CLOUD_CONFIDENTIAL_FORBIDDEN"
                ? "機密圖紙不送雲端"
                : "回答失敗"
          }
          message={error.message}
          code={error.code}
          requestId={error.request_id}
        />
      )}

      {done && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint">
          <FallbackBadge done={done} />
          {done.egress && <EgressBadge egress={done.egress} />}
          <span className="font-medium text-ink-soft">
            {STRATEGY_LABEL[done.strategy_used] ?? done.strategy_used} · {done.model}
          </span>
          <span>首字 {seconds(done.latency_ms.first_token)}</span>
          <span>總計 {seconds(done.latency_ms.total)}</span>
          <span>{twd(done.cost_twd)}</span>
          {!compact && <span>prompt {done.prompt_version}</span>}
          {!compact && <FeedbackButtons requestId={done.request_id} />}
        </div>
      )}

      {showSources && sources && sources.sources.length > 0 && (
        <div className="rounded-lg border border-line bg-paper/60">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-bold text-ink-soft"
          >
            <span>
              參考來源（{sources.sources.length} 段）
              {sources.rearrange && !sources.rearrange.fallback && (
                <span className="font-normal text-ink-faint">
                  {" "}
                  · 由模型從 {sources.rearrange.candidates} 段候選中篩選
                </span>
              )}
            </span>
            <span>{open ? "收合" : "展開"}</span>
          </button>
          {open && (
            <ol className="flex flex-col gap-1 px-2 pb-2">
              {sources.sources.map((s) => (
                <li
                  key={s.chunk_id}
                  ref={(el) => {
                    sourceRefs.current[s.ref] = el;
                  }}
                  className={`rounded-md p-2 text-xs leading-relaxed transition ${
                    activeRef === s.ref ? "bg-seal-soft ring-1 ring-seal/40" : ""
                  }`}
                >
                  <div className="mb-0.5 flex flex-wrap items-center gap-x-2">
                    <span className="font-bold text-seal">[{s.ref}]</span>
                    <span className="font-medium text-ink">
                      〈{s.title ?? s.artwork_title}〉{s.topic}
                    </span>
                    <span className="text-ink-faint">相似度 {s.score.toFixed(2)}</span>
                  </div>
                  <p className={`text-ink-soft ${compact ? "line-clamp-2" : ""}`}>{s.text}</p>
                  {s.source_url ? (
                    <a
                      href={s.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-ink-faint underline hover:text-seal"
                    >
                      出處 · {s.license}
                    </a>
                  ) : (
                    <span className="text-ink-faint">
                      出處：{s.source_label} · {s.license}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
