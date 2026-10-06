import { useState } from "react";
import { api, ApiError, assetUrl } from "../../api/client";
import { useAccounts } from "../../api/hooks";
import type { DoneEvent, SourceItem } from "../../api/sse";
import { STRATEGY_LABEL } from "../../lib/format";
import {
  citeLinks,
  download,
  esc,
  imageDataUrl,
  printHtml,
  reportPage,
  safeName,
  sourcesHtml,
  stamp,
} from "../../lib/export";

/** 一則問答：問題、回答（含 [編號]）、引用的段落、模型與延遲 */
export interface QaTurn {
  question: string;
  text: string;
  sources: SourceItem[];
  done: DoneEvent | null;
}

export interface QaSubject {
  /** 畫作或圖紙的名稱；沒有指定時用「智慧助理問答」 */
  title: string;
  kind: "artwork" | "part" | null;
  id: string | null;
  /** 封面圖：知識庫縮圖或使用者附的照片 */
  imageUrl: string | null;
}

const LEVELS = ["公開", "內部", "機密"];

/** 報告的機密等級：引用的段落裡最高的那一級（圖紙段落是內部或機密） */
export function reportLevel(turns: QaTurn[]): string {
  let best = 0;
  for (const t of turns) for (const s of t.sources) best = Math.max(best, LEVELS.indexOf(s.level ?? "公開"));
  return LEVELS[best];
}

async function qaReportHtml(subject: QaSubject, turns: QaTurn[], who: string): Promise<string> {
  const cover = subject.imageUrl ? await imageDataUrl(subject.imageUrl) : null;
  const level = reportLevel(turns);
  const noun = subject.kind === "part" ? "檢驗紀錄草稿" : subject.kind === "artwork" ? "導覽講稿" : "問答紀錄";
  const body = turns
    .map((t, i) => {
      const anchor = `t${i + 1}`;
      const d = t.done;
      const meta = d
        ? `${STRATEGY_LABEL[d.strategy_used] ?? d.strategy_used}・${d.model}・總計 ${(d.latency_ms.total / 1000).toFixed(1)} 秒・外送 ${d.egress.bytes} bytes・${d.request_id}`
        : "";
      return `<h2>${turns.length > 1 ? `${i + 1}. ` : ""}${esc(t.question)}</h2>
<p class="a">${citeLinks(t.text.trim(), anchor)}</p>
${meta ? `<p class="fine">${esc(meta)}</p>` : ""}
${t.sources.length ? `<h3>參考來源</h3>${sourcesHtml(t.sources.map((s) => ({ ...s, title: s.title ?? s.artwork_title ?? "" })), anchor)}` : ""}`;
    })
    .join("\n");
  return reportPage({
    title: `〈${subject.title}〉${noun}`,
    subtitle: `${turns.length} 則問答・回答只依引用的知識庫段落`,
    level,
    who,
    body: `${cover ? `<div class="row"><img class="photo" src="${cover}" alt=""></div>` : ""}${body}`,
  });
}

/**
 * 匯出問答報告（docs/adr/017）：HTML 檔（圖片內嵌）或直接列印存成 PDF。
 * 畫作是導覽講稿、圖紙是檢驗紀錄草稿；含內部、機密段落時頁首標示等級。匯出前先記一筆稽核。
 */
export function QaExport({
  subject,
  turns,
  label = "匯出報告",
  compact = false,
}: {
  subject: QaSubject;
  turns: QaTurn[];
  label?: string;
  compact?: boolean;
}) {
  const who = useAccounts().data?.current.label ?? "";
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = turns.filter((t) => t.done && !t.done.degraded && t.text.trim());

  const run = async (format: "html" | "print") => {
    setBusy(true);
    setError(null);
    const parts = ready.flatMap((t) => t.sources.map((s) => s.part_id).filter((x): x is string => !!x));
    const refs = [...new Set([...(subject.id ? [subject.id] : []), ...parts])];
    try {
      await api.logExport({ kind: "qa_report", refs: refs.slice(0, 200), rows: ready.length, title: subject.title });
      const html = await qaReportHtml(subject, ready, who);
      if (format === "print") printHtml(html);
      else download(`問答-${safeName(subject.title)}-${stamp()}.html`, html, "text/html;charset=utf-8");
    } catch (e) {
      setError(`沒有匯出：${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  if (!ready.length) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      <button
        type="button"
        disabled={busy}
        onClick={() => void run("html")}
        className={compact ? "link text-xs" : "btn-ghost px-4 py-1.5 text-sm"}
      >
        {label}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => void run("print")}
        className={compact ? "link text-xs" : "btn-ghost px-4 py-1.5 text-sm"}
      >
        列印／存成 PDF
      </button>
      {error && <span className="text-xs text-danger">{error}</span>}
    </span>
  );
}

/** 問答頁的封面：知識庫縮圖，或使用者附的照片 */
export const subjectImage = (thumbUrl: string | null | undefined, imageId: string | null) =>
  imageId ? api.uploadedImageUrl(imageId) : thumbUrl ? assetUrl(thumbUrl) : null;
