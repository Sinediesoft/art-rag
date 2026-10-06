import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, assetUrl, type CompareRow, type ItemComparison } from "../api/client";
import { useAccounts, useArtworks } from "../api/hooks";
import { streamCompareSummary, type CompareSource } from "../api/sse";
import { AnswerText } from "../components/common/CitationTag";
import { ErrorMessage, Loading } from "../components/common/Feedback";
import { seconds } from "../lib/format";
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
  toCsv,
} from "../lib/export";

type Kind = "artwork" | "part";

interface Summary {
  text: string;
  sources: CompareSource[];
  dropped: number;
  status: "idle" | "streaming" | "done" | "error";
  model?: string;
  ms?: number;
  error?: string;
}

const kindOf = (ref: string | null): Kind | null => (ref?.startsWith("part:") ? "part" : ref?.startsWith("artwork:") ? "artwork" : null);

/**
 * 兩件並排比較（docs/adr/017）：兩幅畫或兩張圖紙逐欄並排，不同的格子標色，每格附出處；
 * 可以請本地模型寫一段差異摘要（附引用），整份匯出成報告或 CSV。網址 ?a=artwork:<id>&b=artwork:<id>。
 * 和「比對兩張照片」（找圖上哪裡不同）互補：這裡比的是資料。
 */
export function ItemComparePage() {
  const [params, setParams] = useSearchParams();
  const a = params.get("a");
  const b = params.get("b");
  const me = useAccounts().data?.current;
  const canMfg = me?.domains.includes("mfg") ?? false;
  const [kind, setKind] = useState<Kind>(kindOf(a) ?? kindOf(b) ?? "artwork");
  const artworks = useArtworks();
  const parts = useQuery({ queryKey: ["parts"], queryFn: api.listParts, enabled: canMfg });
  const cmp = useQuery({
    queryKey: ["compare", a, b, me?.id],
    queryFn: () => api.compareItems(a!, b!),
    enabled: !!a && !!b,
  });
  const [onlyDiff, setOnlyDiff] = useState(false);
  const [summary, setSummary] = useState<Summary>({ text: "", sources: [], dropped: 0, status: "idle" });
  const [exportError, setExportError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    // 換了比較對象或身分：摘要作廢
    abort.current?.abort();
    setSummary({ text: "", sources: [], dropped: 0, status: "idle" });
  }, [a, b, me?.id]);

  const set = (key: "a" | "b", ref: string | null) => {
    const next = new URLSearchParams(params);
    if (ref) next.set(key, ref);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const switchKind = (k: Kind) => {
    setKind(k);
    setParams(new URLSearchParams(), { replace: true });
  };
  // 從智慧助理帶著 ?a=part:… 進來：跟著網址切換種類
  const urlKind = kindOf(a) ?? kindOf(b);
  useEffect(() => {
    if (urlKind) setKind(urlKind);
  }, [urlKind]);

  const options =
    kind === "artwork"
      ? (artworks.data?.items ?? []).map((x) => ({ ref: `artwork:${x.id}`, label: `〈${x.title_zh}〉${x.artist_zh}` }))
      : (parts.data?.items ?? []).map((x) => ({
          ref: `part:${x.id}`,
          label: `〈${x.name_zh}〉${x.part_no}${x.confidentiality !== "公開" ? `・${x.confidentiality}` : ""}`,
        }));

  const runSummary = () => {
    if (!a || !b) return;
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setSummary({ text: "", sources: [], dropped: 0, status: "streaming" });
    void streamCompareSummary(
      a,
      b,
      {
        onSources: (e) => setSummary((s) => ({ ...s, sources: e.sources, dropped: e.dropped })),
        onToken: (t) => setSummary((s) => ({ ...s, text: s.text + t })),
        onDone: (e) => setSummary((s) => ({ ...s, status: "done", model: e.model, ms: e.latency_ms.total })),
        onError: (e) => setSummary((s) => ({ ...s, status: "error", error: `${e.message}（${e.code}）` })),
      },
      ctrl.signal,
    );
  };

  const d = cmp.data;
  const exportAs = async (format: "html" | "print" | "csv") => {
    if (!d) return;
    setExportError(null);
    try {
      await api.logExport({
        kind: "compare",
        refs: [d.a.id as string, d.b.id as string],
        rows: d.rows.length,
        title: `${nameOf(d, "a")} vs ${nameOf(d, "b")}`,
      });
    } catch (e) {
      setExportError(`沒有匯出：${e instanceof ApiError ? e.message : String(e)}`);
      return;
    }
    const file = `比較-${safeName(nameOf(d, "a"))}-${safeName(nameOf(d, "b"))}-${stamp()}`;
    if (format === "csv") {
      const lines = d.rows.map((r) => [r.group, r.label, r.a ?? "", r.b ?? "", r.same ? "相同" : "不同", r.source_a.url ?? r.source_a.label, r.source_b.url ?? r.source_b.label]);
      download(`${file}.csv`, toCsv([["分區", "欄位", `甲 ${nameOf(d, "a")}`, `乙 ${nameOf(d, "b")}`, "比較", "甲的出處", "乙的出處"], ...lines]), "text/csv;charset=utf-8");
      return;
    }
    const html = await compareReport(d, summary, me?.label ?? "");
    if (format === "print") printHtml(html);
    else download(`${file}.html`, html, "text/html;charset=utf-8");
  };

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="t-display">兩件並排比較</h1>
        <p className="max-w-3xl text-sm text-ink-80">
          選兩幅畫或兩張圖紙，逐欄並排、不同的格子標色，每格都附出處（知識庫資料、典藏頁、標準模型計算或色彩分析）。
          表格直接讀知識庫、不呼叫模型；需要時可以請本地模型寫一段差異摘要。整份可以匯出成報告或 CSV。
        </p>
      </header>

      <section className="card flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="t-fine font-semibold text-ink-48">比較</span>
          <div className="flex rounded-full border border-hairline bg-card p-1">
            {(["artwork", "part"] as Kind[]).map((k) => (
              <button
                key={k}
                type="button"
                disabled={k === "part" && !canMfg}
                title={k === "part" && !canMfg ? "目前身分不能使用工廠圖紙" : undefined}
                onClick={() => switchKind(k)}
                className={`rounded-full px-3 py-1 text-sm font-normal transition disabled:opacity-40 ${
                  kind === k ? "bg-accent text-white" : "text-ink-80 hover:bg-parchment-deep"
                }`}
              >
                {k === "artwork" ? "兩幅畫" : "兩張圖紙"}
              </button>
            ))}
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {(["a", "b"] as const).map((key) => (
            <label key={key} className="flex flex-col gap-1 text-xs text-ink-48">
              {key === "a" ? "甲" : "乙"}
              <select
                value={(key === "a" ? a : b) ?? ""}
                onChange={(e) => set(key, e.target.value || null)}
                className="field"
              >
                <option value="">（請選擇）</option>
                {options.map((o) => (
                  <option key={o.ref} value={o.ref} disabled={o.ref === (key === "a" ? b : a)}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      </section>

      {!a || !b ? (
        <p className="text-sm text-ink-48">選好甲、乙兩件就會出現比較表。</p>
      ) : cmp.isLoading ? (
        <Loading />
      ) : cmp.error ? (
        <ErrorMessage
          title={(cmp.error as ApiError).status === 403 ? "目前身分看不到其中一件" : "沒辦法比較"}
          message={(cmp.error as Error).message}
          code={(cmp.error as ApiError).code}
          requestId={(cmp.error as ApiError).requestId}
        />
      ) : (
        d && (
          <>
            {d.level !== "公開" && (
              <p className="rounded-lg bg-danger-soft px-3 py-2 text-sm font-semibold text-danger">
                含{d.level}圖紙：匯出的報告與 CSV 會標示{d.level}，請依公司規定保管
              </p>
            )}
            <div className="grid grid-cols-2 gap-3">
              {(["a", "b"] as const).map((key) => (
                <ItemHead key={key} side={key === "a" ? "甲" : "乙"} item={d[key]} kind={d.kind} />
              ))}
            </div>

            <section className="card flex flex-col gap-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm">
                  <b>{d.differences}</b> 個欄位不同・直接讀知識庫 {d.latency_ms} ms・外送 {d.egress.bytes} bytes
                </p>
                <label className="flex items-center gap-1.5 text-sm text-ink-80">
                  <input type="checkbox" checked={onlyDiff} onChange={(e) => setOnlyDiff(e.target.checked)} />
                  只看不同的欄位
                </label>
              </div>
              <CompareTable rows={d.rows} onlyDiff={onlyDiff} />
            </section>

            <section className="card flex flex-col gap-3 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-semibold">差異摘要</h2>
                <button
                  type="button"
                  onClick={runSummary}
                  disabled={summary.status === "streaming"}
                  className="btn-ghost px-4 py-1.5 text-sm"
                >
                  {summary.status === "streaming" ? "本地模型撰寫中…" : summary.status === "idle" ? "請本地模型寫一段" : "重新產生"}
                </button>
              </div>
              {summary.status === "idle" && (
                <p className="text-sm text-ink-48">
                  依比較表與兩邊各幾段知識段落寫成，每句附引用；只用本地模型（約 10–40 秒），有洩密風險的段落先剔除。
                </p>
              )}
              {summary.text && (
                <p className={`whitespace-pre-wrap leading-relaxed ${summary.status === "streaming" ? "caret" : ""}`}>
                  <AnswerText text={summary.text.trim()} />
                </p>
              )}
              {summary.status === "error" && <p className="text-sm text-danger">{summary.error}</p>}
              {summary.status === "done" && (
                <p className="text-xs text-ink-48">
                  {summary.model}・{seconds(summary.ms)}・外送 0 bytes
                  {summary.dropped > 0 && `・剔除 ${summary.dropped} 段有洩密風險的段落`}
                </p>
              )}
              {summary.sources.length > 0 && (
                <ol className="flex flex-col gap-1 text-xs">
                  {summary.sources.map((s) => (
                    <li key={s.chunk_id} className="rounded-md bg-parchment p-2">
                      <span className="font-semibold text-accent">[{s.ref}]</span> {s.side}〈{s.title}〉{s.topic}
                      <p className="line-clamp-2 text-ink-80">{s.text}</p>
                    </li>
                  ))}
                </ol>
              )}
            </section>

            <section className="flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => void exportAs("html")} className="btn-primary">
                匯出報告
              </button>
              <button type="button" onClick={() => void exportAs("print")} className="btn-ghost">
                列印／存成 PDF
              </button>
              <button type="button" onClick={() => void exportAs("csv")} className="btn-ghost">
                匯出 CSV
              </button>
              <span className="text-xs text-ink-48">
                {summary.status === "done" ? "報告會附上差異摘要與引用" : "產生差異摘要後再匯出，報告會一併附上"}
              </span>
            </section>
            {exportError && <p className="rounded-lg bg-danger-soft p-2 text-sm text-danger">{exportError}</p>}
          </>
        )
      )}
    </div>
  );
}

const nameOf = (d: ItemComparison, key: "a" | "b") =>
  String(d.kind === "artwork" ? d[key].title_zh : d[key].name_zh);

function ItemHead({ side, item, kind }: { side: string; item: ItemComparison["a"]; kind: Kind }) {
  const title = String(kind === "artwork" ? item.title_zh : item.name_zh);
  const sub = kind === "artwork" ? `${item.artist_zh}・${item.date_text}` : `${item.part_no}・rev.${item.revision}`;
  const to = kind === "artwork" ? `/artworks/${item.id}` : `/drawings/${item.id}`;
  return (
    <Link to={to} className="card flex items-center gap-3 p-2 pr-3 transition hover:border-ink-48">
      <img src={assetUrl(String(item.thumb_url))} alt="" className="h-14 w-14 shrink-0 rounded-lg object-cover" />
      <div className="min-w-0">
        <p className="text-xs font-semibold text-accent">{side}</p>
        <p className="truncate font-semibold">〈{title}〉</p>
        <p className="truncate text-xs text-ink-48">{sub}</p>
      </div>
    </Link>
  );
}

function SourceNote({ s }: { s: CompareRow["source_a"] }) {
  return s.url ? (
    <a href={s.url} target="_blank" rel="noreferrer" className="block text-[11px] text-ink-48 underline hover:text-accent">
      {s.label}
    </a>
  ) : (
    <span className="block text-[11px] text-ink-48">{s.label}</span>
  );
}

function CompareTable({ rows, onlyDiff }: { rows: CompareRow[]; onlyDiff: boolean }) {
  const visible = rows.filter((r) => !onlyDiff || (!r.same && (r.a || r.b)));
  const groups: [string, CompareRow[]][] = [];
  for (const r of visible) {
    const last = groups[groups.length - 1];
    if (last && last[0] === r.group) last[1].push(r);
    else groups.push([r.group, [r]]);
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead>
          <tr className="border-b border-hairline text-left text-xs text-ink-48">
            <th className="w-28 py-1.5 font-normal">欄位</th>
            <th className="py-1.5 font-normal">甲</th>
            <th className="py-1.5 font-normal">乙</th>
          </tr>
        </thead>
        <tbody>
          {groups.map(([group, rs]) => (
            <GroupRows key={group} group={group} rows={rs} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GroupRows({ group, rows }: { group: string; rows: CompareRow[] }) {
  return (
    <>
      <tr>
        <td colSpan={3} className="pt-3 pb-1 text-xs font-semibold text-ink-48">
          {group}
        </td>
      </tr>
      {rows.map((r) => {
        const diff = !r.same && (r.a || r.b);
        return (
          <tr key={r.key} className="border-b border-hairline/60 align-top">
            <td className="py-1.5 pr-2 text-ink-80">{r.label}</td>
            {(["a", "b"] as const).map((k) => (
              <td key={k} className={`py-1.5 pr-2 ${diff ? "bg-warning-soft" : ""}`}>
                <span className="break-words">{r[k] ?? <span className="text-ink-48">—</span>}</span>
                <SourceNote s={k === "a" ? r.source_a : r.source_b} />
              </td>
            ))}
          </tr>
        );
      })}
    </>
  );
}

/** 比較報告：兩件的縮圖、比較表（不同的格子標色、每格出處）、差異摘要與引用 */
async function compareReport(d: ItemComparison, summary: Summary, who: string): Promise<string> {
  const [ta, tb] = await Promise.all([
    imageDataUrl(assetUrl(String(d.a.thumb_url))),
    imageDataUrl(assetUrl(String(d.b.thumb_url))),
  ]);
  const na = nameOf(d, "a");
  const nb = nameOf(d, "b");
  const src = (s: CompareRow["source_a"]) =>
    `<div class="fine">${s.url ? `<a href="${esc(s.url)}">${esc(s.label)}</a>` : esc(s.label)}</div>`;
  let group = "";
  const rows = d.rows
    .map((r) => {
      const head = r.group !== group ? `<tr class="grp"><td colspan="3">${esc((group = r.group))}</td></tr>` : "";
      const cls = !r.same && (r.a || r.b) ? ' class="diff"' : "";
      return `${head}<tr><td>${esc(r.label)}</td><td${cls}>${esc(r.a ?? "—")}${src(r.source_a)}</td><td${cls}>${esc(r.b ?? "—")}${src(r.source_b)}</td></tr>`;
    })
    .join("");
  const thumbs = [
    [ta, "甲", na],
    [tb, "乙", nb],
  ]
    .map(([img, side, name]) => `<div>${img ? `<img class="photo" src="${img}" alt="">` : ""}<p class="meta">${side}〈${esc(name)}〉</p></div>`)
    .join("");
  const summaryHtml =
    summary.status === "done" && summary.text
      ? `<h2>差異摘要</h2><p class="a">${citeLinks(summary.text.trim(), "s")}</p>
<p class="fine">本地模型 ${esc(summary.model)} 依比較表與下列段落撰寫${summary.dropped ? `；已剔除 ${summary.dropped} 段有洩密風險的段落` : ""}</p>
${sourcesHtml(summary.sources.map((s) => ({ ...s, title: `${s.side}・${s.title}` })), "s")}`
      : "";
  return reportPage({
    title: `〈${na}〉與〈${nb}〉比較`,
    subtitle: `${d.kind === "artwork" ? "兩幅畫" : "兩張圖紙"}並排比較・${d.differences} 個欄位不同`,
    level: d.level,
    who,
    body: `<div class="row">${thumbs}</div>
<h2>比較表</h2><p class="fine">底色標示的是兩邊不同的欄位；每格下方是出處。</p>
<table><thead><tr><th style="width:20%">欄位</th><th>甲〈${esc(na)}〉</th><th>乙〈${esc(nb)}〉</th></tr></thead><tbody>${rows}</tbody></table>
${summaryHtml}`,
  });
}
