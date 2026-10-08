import { useEffect, useRef, useState } from "react";
import { assetUrl } from "../../api/client";
import { MODULE, type Domain } from "../../shell/design";
import { KIND_LABEL, thumbOf, type Output } from "../../shell/outputs";
import { Icon } from "./Icons";
import { ArtworkView, DetailView, DrawingView, ModelView, ScheduleView, SimilarView, StockView, TimelineView } from "./Viewers";

/** 表格下載成 CSV（Excel 開得了：加 BOM）；檔案在瀏覽器裡產生，下載後立刻釋放，不留本機檔案網址 */
function csvOf(columns: string[], rows: (string | number | null)[][]) {
  const cell = (v: string | number | null) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const text = "﻿" + [columns, ...rows].map((r) => r.map(cell).join(",")).join("\n");
  return URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
}

function download(href: string, name: string, revoke: boolean) {
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.click();
  if (revoke) setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function downloadOf(o: Output): (() => void) | null {
  switch (o.kind) {
    case "drawing":
      return o.partId ? () => download(assetUrl(`/api/v1/parts/${encodeURIComponent(o.partId!)}/drawing`), `${o.partId}.png`, false) : null;
    case "artwork":
    case "detail":
      return o.artworkId ? () => download(assetUrl(`/api/v1/artworks/${encodeURIComponent(o.artworkId!)}/image`), `${o.artworkId}.jpg`, false) : null;
    case "model": {
      const stl = o.job?.result?.files["model.stl"];
      return stl ? () => download(assetUrl(stl), `${o.partId ?? "model"}.stl`, false) : null;
    }
    case "stock":
    case "query": {
      const r = o.sql?.result;
      if (!r) return null;
      return () => download(csvOf(r.columns, r.rows), `${o.title.slice(0, 40)}.csv`, true);
    }
    default:
      return null;
  }
}

function Thumb({ o }: { o: Output }) {
  const img = thumbOf(o);
  if ((o.kind === "drawing" || o.kind === "artwork" || o.kind === "detail") && img) return <img src={assetUrl(img)} alt="" className={o.kind === "drawing" ? "is-paper" : ""} />;
  if (o.kind === "similar" && o.items?.length)
    return (
      <span className="thumb-stack">
        {o.items.slice(0, 3).map((x) => (
          <img key={x.artwork.id} src={assetUrl(x.artwork.thumb_url)} alt="" />
        ))}
      </span>
    );
  if (o.kind === "similar" && img) return <img src={assetUrl(img)} alt="" />;
  if (o.kind === "schedule")
    return (
      <svg viewBox="0 0 60 40" className="thumb-glyph" aria-hidden>
        <rect x="6" y="8" width="22" height="5" rx="1.5" style={{ fill: "var(--series-1)" }} />
        <rect x="18" y="17" width="28" height="5" rx="1.5" style={{ fill: "var(--series-2)" }} />
        <rect x="10" y="26" width="18" height="5" rx="1.5" style={{ fill: "var(--series-3)" }} />
        <rect x="31" y="26" width="20" height="5" rx="1.5" style={{ fill: "var(--series-4)" }} />
      </svg>
    );
  if (o.kind === "stock" || o.kind === "query")
    return (
      <svg viewBox="0 0 60 40" className="thumb-glyph" aria-hidden>
        <rect x="8" y="8" width="38" height="5" rx="1.5" style={{ fill: "var(--series-1)" }} />
        <rect x="8" y="17" width="24" height="5" rx="1.5" style={{ fill: "var(--series-1)" }} />
        <rect x="8" y="26" width="14" height="5" rx="1.5" style={{ fill: "var(--series-1)" }} />
      </svg>
    );
  if (o.kind === "timeline")
    return (
      <svg viewBox="0 0 60 40" className="thumb-glyph" aria-hidden>
        <line x1="6" y1="20" x2="54" y2="20" className="thumb-line" />
        {[10, 22, 34, 48].map((x, i) => (
          <circle key={x} cx={x} cy="20" r={i === 2 ? 4 : 2.5} className={i === 2 ? "thumb-dot is-on" : "thumb-dot"} />
        ))}
      </svg>
    );
  return (
    <span className="thumb-model">
      <Icon name="cube" strokeWidth={1.4} />
    </span>
  );
}

/**
 * 來源：ArtRAG-前端demo/source/src/components/Showcase.tsx。
 * 展示區：模組右半邊。只顯示對話產生的成果（目前的一張放大），下方一排縮圖可以切回之前的結果（← →）；
 * 成果跟著對話紀錄，不會消失。
 */
export function Showcase({
  domain,
  outputs,
  active,
  onSelect,
  onAsk,
  onRerun,
}: {
  domain: Domain;
  outputs: Output[];
  active: Output | undefined;
  onSelect: (key: string) => void;
  onAsk: (q: string) => void;
  /** 從紀錄還原、內容沒有保存的那一輪：以目前身分重新查詢 */
  onRerun?: (turnId: string) => void;
}) {
  const root = useRef<HTMLElement>(null);
  const strip = useRef<HTMLOListElement>(null);
  const [full, setFull] = useState(false);
  const idx = active ? outputs.findIndex((o) => o.key === active.key) : -1;
  const go = (d: number) => {
    if (!outputs.length) return;
    const n = (idx + d + outputs.length) % outputs.length;
    onSelect(outputs[n].key);
  };

  useEffect(() => {
    const h = () => setFull(document.fullscreenElement === root.current);
    document.addEventListener("fullscreenchange", h);
    return () => document.removeEventListener("fullscreenchange", h);
  }, []);

  // 新成果出現時，縮圖列捲到它
  useEffect(() => {
    strip.current?.querySelector(".strip__item.is-on")?.scrollIntoView?.({ block: "nearest", inline: "nearest", behavior: "smooth" });
  }, [active?.key]);

  const dl = active ? downloadOf(active) : null;

  return (
    <section
      ref={root}
      className={`showcase showcase--${domain}`}
      aria-label="展示區"
      tabIndex={-1}
      onKeyDown={(e) => {
        if ((e.target as HTMLElement).closest("input, textarea, select, [role=separator]")) return;
        if (e.key === "ArrowLeft") {
          e.preventDefault();
          go(-1);
        }
        if (e.key === "ArrowRight") {
          e.preventDefault();
          go(1);
        }
      }}
    >
      {active ? (
        <>
          <header className="showcase__head">
            <div className="showcase__titles">
              <p className="showcase__kind">
                <span className="showcase__dot" />
                {KIND_LABEL[active.kind]}
              </p>
              <h2 className="showcase__title">{active.title}</h2>
              <p className="showcase__meta">{active.meta}</p>
            </div>
            <div className="showcase__tools">
              {dl && (
                <button type="button" className="tool" title="下載" onClick={dl}>
                  <Icon name="download" strokeWidth={1.6} />
                  <span>下載</span>
                </button>
              )}
              <button
                type="button"
                className="tool"
                title={full ? "離開全螢幕" : "全螢幕"}
                onClick={() => (full ? void document.exitFullscreen() : void root.current?.requestFullscreen?.())}
              >
                <Icon name={full ? "fit" : "expand"} strokeWidth={1.6} />
                <span>{full ? "離開全螢幕" : "全螢幕"}</span>
              </button>
            </div>
          </header>
          <div className="showcase__body" key={active.key}>
            {active.kind === "drawing" && <DrawingView o={active} />}
            {(active.kind === "stock" || active.kind === "query") && <StockView o={active} onRerun={onRerun} />}
            {active.kind === "model" && <ModelView o={active} />}
            {active.kind === "schedule" && <ScheduleView o={active} />}
            {active.kind === "artwork" && <ArtworkView o={active} />}
            {active.kind === "detail" && <DetailView o={active} />}
            {active.kind === "timeline" && <TimelineView o={active} onAsk={onAsk} />}
            {active.kind === "similar" && <SimilarView o={active} onAsk={onAsk} />}
          </div>
        </>
      ) : (
        <div className="showcase__empty">
          <p className="showcase__kind">
            <span className="showcase__dot" />
            {MODULE[domain].label}
          </p>
          <p className="showcase__empty-title">對話產生的成果會大張顯示在這裡</p>
          <p className="showcase__empty-text">
            在左邊提出需求，例如
            {domain === "factory" ? "「連接法蘭有哪些公差要求？」「法蘭還剩幾件可以出貨？」「重新排程」「把法蘭轉成 3D」" : "「介紹谿山行旅圖」「細看這幅畫的技法」「看看這幅畫的年表」「有沒有風格相近的作品？」"}。
          </p>
        </div>
      )}

      <footer className="strip">
        <p className="strip__label">這段對話的成果</p>
        <ol ref={strip} className="strip__list">
          {outputs.map((o, i) => (
            <li key={o.key}>
              <button
                type="button"
                className={`strip__item${o.key === active?.key ? " is-on" : ""}`}
                onClick={() => onSelect(o.key)}
                title={`${KIND_LABEL[o.kind]}・${o.title}`}
                aria-current={o.key === active?.key ? "true" : undefined}
              >
                <span className="strip__thumb">
                  <Thumb o={o} />
                </span>
                <span className="strip__text">
                  <span className="strip__n num">{String(i + 1).padStart(2, "0")}</span>
                  {KIND_LABEL[o.kind]}
                </span>
              </button>
            </li>
          ))}
        </ol>
        <div className="strip__nav">
          <button type="button" onClick={() => go(-1)} disabled={outputs.length < 2} title="上一個（←）" aria-label="上一個成果">
            <Icon name="chevronLeft" strokeWidth={1.6} />
          </button>
          <span className="num">
            {String(Math.max(0, idx + 1)).padStart(2, "0")} / {String(outputs.length).padStart(2, "0")}
          </span>
          <button type="button" onClick={() => go(1)} disabled={outputs.length < 2} title="下一個（→）" aria-label="下一個成果">
            <Icon name="chevronRight" strokeWidth={1.6} />
          </button>
        </div>
      </footer>
    </section>
  );
}
