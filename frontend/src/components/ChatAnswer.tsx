import { useEffect, useRef, useState } from "react";
import { api, assetUrl, type ChatRequest } from "../api/client";
import type { DoneEvent, ErrorEvent, PostFilterInfo, SourcesEvent } from "../api/sse";
import { useChatStream } from "../hooks/useChatStream";
import { seconds, STRATEGY_LABEL, twd } from "../lib/format";
import { AnswerText } from "./common/CitationTag";
import { ErrorMessage, FeedbackButtons } from "./common/Feedback";
import { QaExport } from "./common/QaExport";
import { EgressBadge, FallbackBadge, NotInKbNotice } from "./common/StatusNotices";

/** 一次問答：開始串流、顯示引用、來源、延遲與成本。問答頁、策略比較頁、智慧助理共用。
 * onProgress：智慧助理用來畫七段權限控管的第 3～7 段（檢索、驗證、重排、閘門、生成）；
 * 問答頁用來收集整段問答匯出報告（text 在 done 時是完整的回答）。
 * 不是 compact 時，回答完成後可以匯出這一則的報告（docs/adr/017）。 */
export function ChatAnswer({
  request,
  compact = false,
  showSources = true,
  onProgress,
}: {
  request: ChatRequest;
  compact?: boolean;
  showSources?: boolean;
  onProgress?: (p: {
    sources: SourcesEvent | null;
    done: DoneEvent | null;
    error: ErrorEvent | null;
    text: string;
  }) => void;
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
  const progress = useRef(onProgress);
  progress.current = onProgress;
  const textRef = useRef(text);
  textRef.current = text; // 不放進依賴：每個字都通知會讓整頁跟著重畫；done 時已是完整回答
  useEffect(() => {
    progress.current?.({ sources, done, error, text: textRef.current });
  }, [sources, done, error]);

  if (error?.code === "NOT_IN_KB") return <NotInKbNotice />;

  return (
    <div className="flex flex-col gap-3">
      {status === "retrieving" && (
        <p className="flex items-center gap-2 text-sm text-ink-80">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-hairline border-t-accent" />
          檢索知識庫中…
        </p>
      )}
      {status === "streaming" && !text && (
        <p className="flex items-center gap-2 text-sm text-ink-80">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-hairline border-t-accent" />
          已取回 {sources?.sources.length ?? 0} 段資料，等待模型回答…
        </p>
      )}
      {sources?.post_filter && <ContextNote pf={sources.post_filter} drawing={!!sources.part_id} />}
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
                : error.code === "DATA_SCOPE_DENIED"
                  ? "目前身分看不到這份資料"
                  : "回答失敗"
          }
          message={error.message}
          code={error.code}
          requestId={error.request_id}
        />
      )}

      {done?.degraded && (
        <p className="text-xs text-ink-48">
          🔒 第 6 段生成閘門判斷權限內沒有可答的內容，回覆「查無資料」，沒有呼叫 LLM
          {done.egress.jev_bytes ? `・外送 ${(done.egress.jev_bytes / 1024).toFixed(1)} KB → Jev（代號化公開段落）` : "・全程地端"}
        </p>
      )}
      {done && !done.degraded && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-48">
          <FallbackBadge done={done} />
          {done.egress && <EgressBadge egress={done.egress} />}
          <span className="font-normal text-ink-80">
            {STRATEGY_LABEL[done.strategy_used] ?? done.strategy_used} · {done.model}
          </span>
          <span>首字 {seconds(done.latency_ms.first_token)}</span>
          <span>總計 {seconds(done.latency_ms.total)}</span>
          <span>{twd(done.cost_twd)}</span>
          {!compact && <span>prompt {done.prompt_version}</span>}
          {!compact && <FeedbackButtons requestId={done.request_id} />}
          {!compact && (
            <QaExport
              compact
              subject={{
                title: sources?.sources[0]?.title ?? "智慧助理問答",
                kind: request.part_id ? "part" : request.artwork_id ? "artwork" : null,
                id: request.part_id ?? request.artwork_id ?? null,
                imageUrl: subjectThumb(request),
              }}
              turns={[{ question: request.question, text, sources: sources?.sources ?? [], done }]}
            />
          )}
        </div>
      )}

      {showSources && sources && sources.sources.length > 0 && (
        <div className="rounded-lg border border-hairline bg-parchment">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-semibold text-ink-80"
          >
            <span>
              參考來源（{sources.sources.length} 段）
              {/* 只有 0～1 段候選時後端不呼叫模型（candidates ≤ 1、ms 為 0），不算篩選過 */}
              {sources.rearrange && !sources.rearrange.fallback && sources.rearrange.candidates > 1 && (
                <span className="font-normal text-ink-48">
                  {" "}
                  · 由模型從 {sources.rearrange.candidates} 段候選中篩選
                </span>
              )}
            </span>
            <span className="font-normal text-accent">{open ? "收合" : "展開"}</span>
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
                    activeRef === s.ref ? "bg-accent-soft ring-1 ring-accent/40" : ""
                  }`}
                >
                  <div className="mb-0.5 flex flex-wrap items-center gap-x-2">
                    <span className="font-semibold text-accent">[{s.ref}]</span>
                    <span className="font-normal text-ink">
                      〈{s.title ?? s.artwork_title}〉{s.topic}
                    </span>
                    <span className="text-ink-48">相似度 {s.score.toFixed(2)}</span>
                  </div>
                  <p className={`text-ink-80 ${compact ? "line-clamp-2" : ""}`}>{s.text}</p>
                  {s.source_url ? (
                    <a
                      href={s.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-ink-48 underline hover:text-accent"
                    >
                      出處 · {s.license}
                    </a>
                  ) : (
                    <span className="text-ink-48">
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

/** 放在回答上方的小註記：上下文是怎麼來的、有沒有剔除洩密段落（七段權限控管第 4～6 段） */
function ContextNote({ pf, drawing }: { pf: PostFilterInfo; drawing: boolean }) {
  if (pf.gate && !pf.gate.passed) return null; // 降級回應本身就是答案
  const parts = [`上下文 ${pf.kept} 段`];
  if (pf.mode !== "scan")
    parts[0] += `（從 ${pf.candidates} 段候選經${pf.engine === "jev" ? " Jev Noul 驗證、Jev Score 重排" : "地端驗證、重排"}）`;
  if (pf.flagged.length)
    parts.push(`已剔除 ${pf.flagged.length} 段有洩密風險的段落（${pf.flagged.map((x) => x.topic).join("、")}）`);
  if (drawing && pf.mode !== "scan") parts.push("機密圖紙只用地端模型");
  if (pf.mode === "scan" && !pf.flagged.length) return null;
  return (
    <p
      className={`rounded-lg px-2.5 py-1.5 text-xs ${pf.flagged.length ? "bg-warning-soft text-warning" : "bg-parchment text-ink-48"}`}
    >
      {parts.join("；")}
    </p>
  );
}

/** 報告封面：使用者附的照片，或知識庫的縮圖 */
function subjectThumb(r: ChatRequest): string | null {
  if (r.image_id) return api.uploadedImageUrl(r.image_id);
  if (r.part_id) return assetUrl(`/api/v1/parts/${encodeURIComponent(r.part_id)}/drawing?size=thumb`);
  if (r.artwork_id) return assetUrl(`/api/v1/artworks/${encodeURIComponent(r.artwork_id)}/image?size=thumb`);
  return null;
}
