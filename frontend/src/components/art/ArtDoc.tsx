import { useEffect, useId, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { assetUrl, type ApiError, type ArtworkDetail, type ArtworkSummary } from "../../api/client";
import { useArtwork, useArtworks, useImageSearch } from "../../api/hooks";
import { whereOnPainting } from "../../lib/format";
import type { Output } from "../../shell/outputs";
import type { Turn } from "../../shell/types";
import { Icon, Spinner } from "../shell/Icons";
import { ColorSection } from "./ArtColor";
import { CompareSection } from "./ArtCompare";
import { polygonArea, type Pt } from "./geometry";

/**
 * 藝術模組中間的文件（mockups/painting-module.html）：一份作品成果由上而下四段——
 * 檢索到的圖片 → 畫作資料＋區域資料 → 色溫 → 影像對位與差異（只有附照片時）。
 * 內容全部讀知識庫與分析 API（/artworks/{id}、/search/image、/colors、/align），不經過生成模型；
 * 回答引用過的段落標出 [n]，左邊對話點了 [n] 會捲到這裡的那一段。
 */

const short = (s: string) => s.split("（")[0];
const LANG: Record<string, string> = { zh: "中文", en: "英文", ja: "日文" };

/** 這幅畫在這段對話裡被檢索到的段落：chunk_id → 引用編號、相似度、回答有沒有真的引用 */
export type CiteMap = Record<string, { ref: number; score: number; cited: boolean }>;

/** 段落編號：chunk_id 是 <作品 id>#<說明序號兩位數>-<切段序號>（backend/app/rag/chunking.py），另有 #meta、#color */
export function anchorOf(chunk: string): string | null {
  const m = chunk.match(/#(\d{2})-\d+$/);
  if (m) return `aw-p-${Number(m[1])}`;
  if (chunk.endsWith("#meta")) return "aw-rec-meta";
  if (chunk.endsWith("#color")) return "aw-sec-color";
  return null;
}

export function ArtDoc({
  o,
  when,
  cites,
  focus,
  onOpen,
  onAsk,
}: {
  o: Output;
  /** 這份成果是哪個時間問的（對話的那一輪） */
  when?: number;
  cites: CiteMap;
  /** 對話裡點了引用：捲到那一段並閃一下（seq 變了就再捲一次） */
  focus: { chunk: string; seq: number } | null;
  onOpen: (artworkId: string) => void;
  onAsk: (q: string) => void;
}) {
  const { data: a, error, isLoading } = useArtwork(o.artworkId);
  const [flash, setFlash] = useState<{ id: string; seq: number } | null>(null);

  useEffect(() => {
    if (!focus || !a) return;
    const id = anchorOf(focus.chunk);
    if (!id) return;
    const el = document.getElementById(id);
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    setFlash({ id, seq: focus.seq });
    const t = window.setTimeout(() => setFlash(null), 1700);
    return () => window.clearTimeout(t);
  }, [focus?.seq, a]);

  if (isLoading)
    return (
      <article className="aw-doc">
        <p className="aw-pending">
          <Spinner />
          讀取作品…
        </p>
      </article>
    );
  if (error || !a) {
    const e = error as ApiError | null;
    return (
      <article className="aw-doc">
        <p className="aw-error" role="alert">
          <Icon name="lock" />
          {e?.status === 403 ? "目前身分看不到這份資料" : e?.status === 404 ? "找不到這幅畫" : "讀取失敗"}
          {e?.message ? `：${e.message}` : ""}
        </p>
      </article>
    );
  }
  const fl = (id: string) => (flash?.id === id ? " is-flash" : "");
  return (
    <article className="aw-doc">
      <p className="aw-subject__code">kb / artworks / {a.id}</p>
      <h1 className="aw-subject__title">〈{a.title.zh}〉</h1>
      <p className="aw-subject__by">
        {[
          a.artist.en ? `${a.artist.zh}（${a.artist.en}）` : a.artist.zh,
          short(a.date_text),
          o.imageId ? "比對你的照片" : null,
          when ? new Date(when).toLocaleString("zh-TW", { hour12: false, dateStyle: "short", timeStyle: "short" }) : null,
        ]
          .filter(Boolean)
          .join("・")}
      </p>

      <HitsSection a={a} imageId={o.imageId} onOpen={onOpen} />
      <RecordSection a={a} cites={cites} fl={fl} />
      <ColorSection artworkId={a.id} imageId={o.imageId} thumb={a.thumb_url} focus={flash?.id === "aw-sec-color" ? flash.seq : undefined} />
      {o.imageId && <CompareSection artworkId={a.id} imageId={o.imageId} title={a.title.zh} onAsk={onAsk} />}
    </article>
  );
}

function SecHead({ title, en, children }: { title: string; en: string; children?: ReactNode }) {
  return (
    <div className="aw-sec__head">
      <p className="aw-sec__kind">
        <span className="aw-dot" />
        <b>{title}</b>
        <span className="aw-en">{en}</span>
      </p>
      {children && <div className="aw-sec__tools">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- 01 檢索到的圖片

function HitsSection({ a, imageId, onOpen }: { a: ArtworkDetail; imageId?: string; onOpen: (id: string) => void }) {
  const search = useImageSearch(imageId ?? null);
  const list = useArtworks();
  const hit = search.data?.results.find((r) => r.artwork.id === a.id);
  const others: { art: ArtworkSummary; meta: string }[] = imageId
    ? (search.data?.results ?? []).filter((r) => r.artwork.id !== a.id).map((r) => ({ art: r.artwork, meta: `相似度 ${r.score.toFixed(3)}・對應點 ${r.inliers ?? "—"}` }))
    : (list.data?.items ?? []).filter((x) => x.artist_zh === a.artist.zh && x.id !== a.id).map((x) => ({ art: x, meta: short(x.date_text) }));
  return (
    <section className="aw-sec" id="aw-sec-hits">
      <SecHead title={imageId ? "檢索到的圖片" : "作品"} en={imageId ? "Retrieval" : "Artwork"} />
      <div className="aw-card">
        <Figure a={a} />
        <div className="aw-hit-info">
          <div className="aw-hit-status">
            {imageId ? (
              search.isLoading ? (
                <span>
                  <Spinner />
                  讀取辨識分數…
                </span>
              ) : hit ? (
                <>
                  <span className={hit.verified ? "is-ok" : ""}>{hit.verified ? "✓ 辨識成功" : "未通過幾何驗證"}</span>
                  <span>
                    相似度 <b>{hit.score.toFixed(3)}</b>
                  </span>
                  <span>
                    幾何驗證 <b>{hit.inliers ?? "—"}</b> 個對應點
                  </span>
                </>
              ) : (
                <span>照片辨識出這幅畫（第 1 段以圖搜圖）</span>
              )
            ) : (
              <span>知識庫作品</span>
            )}
          </div>
          <dl className="aw-facts">
            <dt>來源</dt>
            <dd className="mono">kb/artworks/{a.id}.json</dd>
            <dt>圖片</dt>
            <dd>
              {a.image.attribution ?? short(a.collection)}・{a.image.license}
            </dd>
          </dl>
        </div>
        {others.length > 0 && (
          <>
            <p className="aw-label aw-view-label">{imageId ? "其他候選" : "同一位畫家的其他作品"}</p>
            <ul className="aw-strip">
              {others.slice(0, 12).map(({ art, meta }) => (
                <li key={art.id}>
                  <button type="button" className="aw-strip__item" onClick={() => onOpen(art.id)} title={`在新分頁開〈${art.title_zh}〉`}>
                    <span className="aw-strip__thumb">
                      <img src={assetUrl(art.thumb_url)} alt="" loading="lazy" />
                    </span>
                    <span className="aw-strip__title">〈{art.title_zh}〉</span>
                    <span className="aw-strip__meta">{meta}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </section>
  );
}

/**
 * 作品掛在展牆上：圖佔整列，展品標籤（象牙白紙卡、半透明）疊在左上角，右上角全螢幕。
 * 全螢幕先蓋滿視窗（.is-full，搬到 <body> 底下：文件的 container query 會把 position: fixed 關在文件裡），再試瀏覽器全螢幕。
 */
function Figure({ a }: { a: ArtworkDetail }) {
  const [full, setFull] = useState(false);
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  // 圖還沒載入時框是空的：展品標籤等圖出來再疊上去，免得擠成一條
  const [img, setImg] = useState<"loading" | "ok" | "error">("loading");
  useEffect(() => {
    if (!full || !node) return;
    node.requestFullscreen?.().catch(() => undefined);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.fullscreenElement && setFull(false);
    const onChange = () => !document.fullscreenElement && setFull(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("fullscreenchange", onChange);
    };
  }, [full, node]);
  const close = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    setFull(false);
  };
  const fine = [a.medium, a.dimensions, short(a.collection)].filter(Boolean).join("・");
  const figure = (
    <div ref={full ? setNode : undefined} className={`aw-figure${full ? " is-full" : ""}`} role={full ? "dialog" : undefined} aria-label={full ? `〈${a.title.zh}〉全螢幕` : undefined}>
      <div className={`aw-figure__frame${img === "ok" ? "" : " is-loading"}`}>
        <img src={assetUrl(a.image_url)} alt={a.title.zh} onLoad={() => setImg("ok")} onError={() => setImg("error")} />
        {img === "loading" && (
          <p className="aw-figure__wait">
            <Spinner />
            讀取原圖…
          </p>
        )}
        {img === "error" && <p className="aw-figure__wait">讀不到原圖</p>}
        <div className="aw-label-card" hidden={img !== "ok"}>
          <p className="aw-label-card__title">{a.title.zh}</p>
          {a.title.en && <p className="aw-label-card__en">{a.title.en}</p>}
          <p className="aw-label-card__by">
            {a.artist.en ? `${a.artist.zh}（${a.artist.en}）` : a.artist.zh}・{short(a.date_text)}
          </p>
          {fine && <p className="aw-label-card__fine">{fine}</p>}
          {a.style_tags.length > 0 && (
            <p className="aw-label-card__tags">
              {a.style_tags.map((t) => (
                <span key={t}>{t}</span>
              ))}
            </p>
          )}
        </div>
        <button type="button" className="aw-figure__full" onClick={() => (full ? close() : setFull(true))} title={full ? "離開全螢幕（Esc）" : "全螢幕觀看"} aria-label={full ? "離開全螢幕" : "全螢幕觀看"}>
          <Icon name={full ? "unfocus" : "expand"} />
        </button>
      </div>
    </div>
  );
  if (!full) return figure;
  return (
    <>
      <div className="aw-figure aw-figure--placeholder" aria-hidden />
      {createPortal(<div className="app--art aw-portal">{figure}</div>, document.body)}
    </>
  );
}

// ---------------------------------------------------------------- 02 畫作資料＋區域資料

function RecordSection({ a, cites, fl }: { a: ArtworkDetail; cites: CiteMap; fl: (id: string) => string }) {
  const regions = a.regions?.items ?? [];
  const ids = new Set(regions.map((r) => r.id));
  const passages = a.descriptions.map((d, i) => ({ d, i })).filter(({ d }) => !d.region || !ids.has(d.region));
  const citeOf = (i: number) => Object.entries(cites).find(([k]) => anchorOf(k) === `aw-p-${i}`)?.[1];
  const meta = Object.entries(cites).find(([k]) => k.endsWith("#meta"))?.[1];
  return (
    <section className="aw-sec" id="aw-sec-record">
      <SecHead title="畫作資料" en="Record">
        <span className="aw-label">
          {passages.length} 段說明{regions.length ? `・${regions.length} 處區域` : ""}
        </span>
      </SecHead>
      <div className="aw-card">
        <p className="aw-record-label">
          <span className="aw-label">基本資料</span>
          {meta && <span className="aw-passage__cite">{meta.cited ? "回答引用" : "檢索"} [{meta.ref}]</span>}
        </p>
        <dl className={`aw-kv${fl("aw-rec-meta")}`} id="aw-rec-meta">
          <Kv k="編號">
            <span className="mono">{a.id}</span>
            {a.source_id && <span className="alt mono">{a.source_id}</span>}
          </Kv>
          <Kv k="標題">
            {a.title.zh}
            {a.title.en && <span className="alt">{a.title.en}</span>}
          </Kv>
          <Kv k="畫家">
            {a.artist.zh}
            {a.artist.en && <span className="alt">{a.artist.en}</span>}
          </Kv>
          <Kv k="年代">{a.date_text}</Kv>
          {a.medium && <Kv k="媒材">{a.medium}</Kv>}
          {a.dimensions && <Kv k="尺寸">{a.dimensions}</Kv>}
          <Kv k="典藏">{a.collection}</Kv>
          {a.style_tags.length > 0 && (
            <Kv k="風格標籤">
              <span className="aw-pills">
                {a.style_tags.map((t) => (
                  <span key={t}>{t}</span>
                ))}
              </span>
            </Kv>
          )}
          <Kv k="原圖">
            <span className="mono">{a.image.path}</span>・{a.image.license}
            {a.image.attribution ? `・${a.image.attribution}` : ""}
          </Kv>
          <Kv k="資料來源">
            <a href={a.source_url} target="_blank" rel="noreferrer" className="aw-link">
              {a.source_url}
            </a>
          </Kv>
        </dl>

        {passages.length > 0 && (
          <p className="aw-record-label">
            <span className="aw-label">說明（{passages.length} 段）</span>
            <span className="aw-en">descriptions</span>
          </p>
        )}
        {passages.map(({ d, i }) => {
          const c = citeOf(i);
          return (
            <div key={i} id={`aw-p-${i}`} className={`aw-passage${fl(`aw-p-${i}`)}`}>
              <p className="aw-passage__head">
                <span className="aw-passage__title">{d.topic ?? "說明"}</span>
                <span className="aw-passage__topic">{LANG[d.lang] ?? d.lang}</span>
                {d.speaker && <span className="aw-passage__topic">{d.speaker}說</span>}
                {c && (
                  <span className="aw-passage__cite">
                    {c.cited ? "回答引用" : "檢索"} [{c.ref}]・相似度 {c.score.toFixed(2)}
                  </span>
                )}
              </p>
              <p className="aw-passage__text">{d.text}</p>
              <p className="aw-credit">
                出處・
                {d.source_url ? (
                  <a href={d.source_url} target="_blank" rel="noreferrer" className="aw-link">
                    {d.source ?? hostOf(d.source_url)}
                  </a>
                ) : (
                  (d.source ?? "知識庫")
                )}
                ・{d.license}
                {d.attribution ? `（署名：${d.attribution}）` : ""}
              </p>
            </div>
          );
        })}
        <p className="aw-credit">直接讀知識庫 kb/artworks/{a.id}.json・外送 0 bytes</p>
      </div>
      {regions.length > 0 && <AreasCard a={a} citeOf={citeOf} fl={fl} />}
    </section>
  );
}

function Kv({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div>
      <dt>{k}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const hostOf = (url: string) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};

/**
 * 區域資料（docs/adr/029）：藝術家在畫上圈一塊、寫解說，主管收錄後才出現。
 * 圖上白線框出區域、金色編號；滑過標亮，點一塊那一塊以外調暗，再點取消。
 */
function AreasCard({ a, citeOf, fl }: { a: ArtworkDetail; citeOf: (i: number) => CiteMap[string] | undefined; fl: (id: string) => string }) {
  const regions = a.regions!.items;
  const [hover, setHover] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const on = hover ?? pinned;
  const maskBase = useId();
  const texts = (id: string) => a.descriptions.map((d, i) => ({ d, i })).filter(({ d }) => d.region === id);
  const bind = (id: string) => ({
    onMouseEnter: () => setHover(id),
    onMouseLeave: () => setHover(null),
    onClick: () => setPinned((p) => (p === id ? null : id)),
  });
  return (
    <div className="aw-card aw-areas-card">
      <p className="aw-record-label">
        <span className="aw-label">區域資料（{regions.length} 處）</span>
        <span className="aw-en">regions</span>
        <span className="aw-label aw-areas__hint">點區域或解說，那一塊以外會調暗</span>
      </p>
      <div className="aw-areas">
        <div className="aw-areas__figure">
          <img src={assetUrl(a.image_url)} alt={`〈${a.title.zh}〉的區域`} />
          <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="aw-areas__svg">
            <defs>
              {regions.map((r, i) => (
                <mask key={r.id} id={`${maskBase}-${i}`} maskContentUnits="userSpaceOnUse">
                  <rect width="1" height="1" fill="#fff" />
                  <polygon points={r.points.map(([x, y]) => `${x},${y}`).join(" ")} fill="#000" />
                </mask>
              ))}
            </defs>
            {regions.map((r, i) => (pinned === r.id ? <rect key={r.id} width="1" height="1" fill="#000" opacity={0.55} mask={`url(#${maskBase}-${i})`} className="aw-area-dim" /> : null))}
            {regions.map((r) => (
              <polygon key={r.id} points={r.points.map(([x, y]) => `${x},${y}`).join(" ")} className={`aw-area-poly${on === r.id ? " is-hl" : ""}`} {...bind(r.id)} />
            ))}
          </svg>
          {regions.map((r, i) => {
            const x = Math.min(...r.points.map((p) => p[0]));
            const y = Math.min(...r.points.map((p) => p[1]));
            return (
              <button key={r.id} type="button" className={`aw-spot${on === r.id ? " is-hl" : ""}`} style={{ left: `${x * 100}%`, top: `${y * 100}%` }} aria-label={`區域 ${i + 1}：${r.label}`} {...bind(r.id)}>
                {i + 1}
              </button>
            );
          })}
        </div>
        <ul className="aw-notes">
          {regions.map((r, i) => {
            const xs = r.points.map((p) => p[0]);
            const ys = r.points.map((p) => p[1]);
            const center = [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
            const area = Math.abs(polygonArea(r.points as Pt[]));
            return (
              <li key={r.id}>
                <div className={`aw-note aw-note--area${on === r.id ? " is-on" : ""}`} role="button" tabIndex={0} aria-pressed={pinned === r.id} {...bind(r.id)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setPinned((p) => (p === r.id ? null : r.id)))}>
                  <span className="aw-n">{i + 1}</span>
                  <span>
                    <span className="aw-note__kind">
                      {r.label}
                      <span className="aw-note__where">畫面{whereOnPainting(center)}</span>
                    </span>
                    {texts(r.id).map(({ d, i: di }) => {
                      const c = citeOf(di);
                      return (
                        <span key={di} id={`aw-p-${di}`} className={`aw-area__text${fl(`aw-p-${di}`)}`}>
                          {d.speaker ? `${d.speaker}說：「${d.text}」` : d.text}
                          {c && (
                            <span className="aw-passage__cite">
                              {" "}
                              {c.cited ? "回答引用" : "檢索"} [{c.ref}]
                            </span>
                          )}
                          <span className="aw-note__ratio">
                            {d.source ?? "知識庫"}・{d.license}
                            {d.attribution ? `（署名：${d.attribution}）` : ""}
                          </span>
                        </span>
                      );
                    })}
                    <span className="aw-note__ratio num">
                      {r.id}・占畫面 {Math.max(1, Math.round(area * 100))}%
                    </span>
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

/** 對話裡這幅畫被檢索到的段落（最近一次）：回答文字裡有 [n] 才算「回答引用」 */
export function citesFor(artworkId: string, turns: Turn[]): CiteMap {
  const out: CiteMap = {};
  for (const t of turns) {
    // 從紀錄還原的那一輪：公開的回答與段落有存（shell/persist.ts）
    const p = t.part;
    const live = p?.kind === "chat" && p.sources ? { text: p.text, sources: p.sources.sources } : null;
    const saved = t.archived?.answer && t.archived.sources ? { text: t.archived.answer, sources: t.archived.sources } : null;
    const r = live ?? saved;
    if (!r) continue;
    for (const s of r.sources) {
      if (s.artwork_id !== artworkId) continue;
      out[s.chunk_id] = { ref: s.ref, score: s.score, cited: r.text.includes(`[${s.ref}]`) };
    }
  }
  return out;
}
