import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { api, ApiError, assetUrl, type ImageAlignment } from "../../api/client";
import { useAccounts, useImageAlignment } from "../../api/hooks";
import { download, esc, imageDataUrl, reportPage, safeName, stamp } from "../../lib/export";
import { whereOnPainting } from "../../lib/format";
import { Icon, Spinner } from "../shell/Icons";
import { useSize } from "../shell/Viewers";
import { ptsAttr, quadTransform, rotationOf, scalePts, type Pt } from "./geometry";

/**
 * 藝術模組「影像對位與差異」（docs/adr/012 的 /images/{id}/align）：同一格三顆切換鈕。
 * - 影像對位：左照片、右原圖，照片四角連到它在原圖上的位置，原圖框出拍到的範圍
 * - 拖曳比對：照片依四角投影回原圖座標（瀏覽器的透視轉換），左右拖曳分隔線對照
 * - 差異疊圖：伺服器畫好的疊圖（差異塗色並編號、沒拍到的地方調暗）
 * 差異框與右邊清單互相連動。全部來自後端的計算，不經過生成模型。
 */

/** 差異種類：顏色和 AlignmentCards.tsx 一樣（紅＝形狀、琥珀＝顏色、紫＝兩者） */
export const DIFF_KIND = {
  shape: { cls: "shape", label: "形狀不同", desc: "加筆、補筆、塗糊或多了東西" },
  color: { cls: "tone", label: "顏色不同", desc: "褪色、補色或反光" },
  both: { cls: "both", label: "形狀和顏色都不同", desc: "可能同時有補筆與補色，或大片反光" },
} as const;

type Region = NonNullable<ImageAlignment["diff"]>["regions"][number];
export type DiffRegion = Region & { kind: keyof typeof DIFF_KIND };
export const diffRegions = (a: ImageAlignment | undefined): DiffRegion[] => (a?.diff?.regions ?? []).filter((r): r is DiffRegion => r.kind in DIFF_KIND);

const pct = (x: number) => `${Math.max(1, Math.round(x * 100))}%`;
const centerOf = (b: number[]) => [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
type Mode = "align" | "swipe" | "overlay";

export function CompareSection({ artworkId, imageId, title, onAsk }: { artworkId: string; imageId: string; title: string; onAsk: (q: string) => void }) {
  const { data, isLoading, error } = useImageAlignment(imageId, `artwork:${artworkId}`);
  const [mode, setMode] = useState<Mode>("align");
  const [lines, setLines] = useState(true);
  const [cover, setCover] = useState(true);
  const [boxes, setBoxes] = useState(true);
  const [hover, setHover] = useState<number | null>(null);
  const [pinned, setPinned] = useState<number | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const { data: accounts } = useAccounts();
  const active = hover ?? pinned;
  const regions = diffRegions(data);
  const photoUrl = api.uploadedImageUrl(imageId);
  const pin = (i: number) => setPinned((p) => (p === i ? null : i));

  const exportReport = async () => {
    if (!data) return;
    setExporting("產生報告中…");
    try {
      await api.logExport({ kind: "compare", refs: [artworkId], rows: regions.length, title: `〈${title}〉與照片比對` });
    } catch (e) {
      setExporting(`沒有匯出：${e instanceof ApiError ? e.message : String(e)}`);
      return;
    }
    const html = await compareReport({ title, data, regions, photoUrl, who: accounts?.current.label ?? "" });
    download(`照片比對-${safeName(title)}-${stamp()}.html`, html, "text/html;charset=utf-8");
    setExporting(null);
  };

  return (
    <section className={`aw-sec mode-${mode}`} id="aw-sec-compare">
      <div className="aw-sec__head">
        <p className="aw-sec__kind">
          <span className="aw-dot" />
          <b>影像對位與差異</b>
          <span className="aw-en">Alignment</span>
        </p>
        <div className="aw-sec__tools" role="radiogroup" aria-label="比對方式">
          {(
            [
              ["align", "影像對位"],
              ["swipe", "拖曳比對"],
              ["overlay", "差異疊圖"],
            ] as const
          ).map(([m, label]) => (
            <button key={m} type="button" role="radio" aria-checked={mode === m} className={`aw-tool${mode === m ? " is-on" : ""}`} onClick={() => setMode(m)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="aw-card">
        {isLoading && (
          <p className="aw-pending">
            <Spinner />
            比對照片拍到原畫的哪一塊、有沒有不一樣…
          </p>
        )}
        {error && <AlignError error={error} />}
        {data && (
          <>
            <div className="aw-cmp-opts">
              {mode === "align" ? (
                <>
                  <label className="aw-check">
                    <input type="checkbox" checked={lines} onChange={(e) => setLines(e.target.checked)} />
                    顯示四角對應
                  </label>
                  <label className="aw-check">
                    <input type="checkbox" checked={cover} onChange={(e) => setCover(e.target.checked)} />
                    顯示拍到範圍
                  </label>
                </>
              ) : (
                <label className="aw-check">
                  <input type="checkbox" checked={boxes} onChange={(e) => setBoxes(e.target.checked)} />
                  差異框
                </label>
              )}
            </div>
            {mode === "align" ? (
              <AlignView data={data} photoUrl={photoUrl} title={title} lines={lines} cover={cover} regions={regions} />
            ) : (
              <div className="aw-diff">
                <div>
                  <Swipe data={data} photoUrl={photoUrl} overlay={mode === "overlay"} boxes={boxes} regions={regions} active={active} onHover={setHover} onPin={pin} />
                  <p className="aw-legend">
                    <span>
                      <i className="shape" />
                      形狀不同
                    </span>
                    <span>
                      <i className="tone" />
                      顏色不同
                    </span>
                    <span>
                      <i className="both" />
                      形狀和顏色都不同
                    </span>
                    <span>
                      <i className="cover" />
                      照片拍到的範圍
                    </span>
                  </p>
                </div>
                <div>
                  <p className="aw-diff__sum">{diffSummary(data, regions.length)}</p>
                  {regions.length > 0 && (
                    <ul className="aw-notes">
                      {regions.map((r, i) => (
                        <li key={r.bbox.join(",")}>
                          <button
                            type="button"
                            className={`aw-note${active === i ? " is-on" : ""}`}
                            onMouseEnter={() => setHover(i)}
                            onMouseLeave={() => setHover(null)}
                            onClick={() => pin(i)}
                            aria-pressed={pinned === i}
                          >
                            <span className={`aw-n ${DIFF_KIND[r.kind].cls}`}>{i + 1}</span>
                            <span>
                              <span className="aw-note__kind">
                                {DIFF_KIND[r.kind].label}
                                <span className="aw-note__where">畫面{whereOnPainting(centerOf(r.bbox))}</span>
                              </span>
                              <span className="aw-note__desc">{DIFF_KIND[r.kind].desc}</span>
                              <span className="aw-note__ratio num">占比對範圍 {pct(r.area_ratio)}</span>
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="aw-actions">
                    {regions.length > 0 && (
                      <button
                        type="button"
                        className="aw-btn"
                        onClick={() => onAsk(`〈${title}〉的${[...new Set(regions.map((r) => whereOnPainting(centerOf(r.bbox))))].join("、")}一帶，修復紀錄或說明有提到嗎？`)}
                        title="送進左邊的對話，查知識庫有沒有提到這幾處"
                      >
                        問問這幾處
                      </button>
                    )}
                    <button type="button" className="aw-btn aw-btn--ghost" onClick={() => void exportReport()} disabled={exporting === "產生報告中…"}>
                      匯出報告
                    </button>
                  </div>
                  {exporting && <p className="aw-credit">{exporting}</p>}
                </div>
              </div>
            )}
            <p className="aw-credit">
              {data.notes.map((n) => (
                <span key={n}>
                  {n}
                  <br />
                </span>
              ))}
              ORB 特徵點＋RANSAC・對應點 {data.inliers}・{data.latency_ms} ms・系統計算，不經過生成模型
            </p>
          </>
        )}
      </div>
    </section>
  );
}

function AlignError({ error }: { error: unknown }) {
  const e = error as ApiError | null;
  if (e?.code === "ALIGN_FAILED") return <p className="aw-empty">照片和知識庫原圖對不上（對應的特徵點不夠），無法標出位置，也沒有比對差異。</p>;
  return (
    <p className="aw-error" role="alert">
      影像比對失敗：{e?.message}
      {e?.code ? `（${e.code}）` : ""}
    </p>
  );
}

function diffSummary(data: ImageAlignment, n: number) {
  const d = data.diff;
  if (!d) return "這次只標出位置，沒有比對差異。";
  return {
    same: "拍到的範圍裡，和知識庫原圖一致，沒有找到不一樣的地方。",
    changed: `找到 ${n} 處和知識庫原圖不一樣的地方（差異候選，請對照原圖確認）。`,
    global_change: "和原圖的差異遍布整張：可能光線差太多、大片反光，或照片太模糊，沒有逐處標出。",
  }[d.status];
}

function locationText(data: ImageAlignment, aspect: number | null) {
  const { coverage, center, polygon } = data.location;
  const where = coverage >= 0.9 ? "照片拍到整幅畫" : `照片拍到原畫約 ${pct(coverage)} 的範圍，在畫面的${whereOnPainting(center)}`;
  const deg = aspect ? rotationOf(polygon, aspect) : null;
  const turn = deg == null ? "" : Math.abs(deg) < 0.5 ? "，幾乎沒有旋轉" : `，相對原圖旋轉約 ${Math.abs(deg).toFixed(1)}°`;
  return `${where}${turn}。`;
}

// ---------------------------------------------------------------- 影像對位：左照片、右原圖

const LINE_COLORS = ["var(--series-1)", "var(--mod)", "var(--series-4)", "var(--series-3)"];

function AlignView({ data, photoUrl, title, lines, cover, regions }: { data: ImageAlignment; photoUrl: string; title: string; lines: boolean; cover: boolean; regions: DiffRegion[] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const photo = useRef<HTMLImageElement>(null);
  const ref = useRef<HTMLImageElement>(null);
  const clip = useId();
  const mask = useId();
  type Box = { x: number; y: number; w: number; h: number };
  const [geo, setGeo] = useState<{ w: number; h: number; p: Box; r: Box } | null>(null);
  const [aspect, setAspect] = useState<number | null>(null);
  const measure = useCallback(() => {
    const W = wrap.current;
    const P = photo.current;
    const R = ref.current;
    if (!W || !P || !R || !P.naturalWidth || !R.naturalWidth) return;
    setAspect(R.naturalWidth / R.naturalHeight);
    const o = W.getBoundingClientRect();
    const rel = (el: HTMLElement): Box => {
      const b = el.getBoundingClientRect();
      return { x: b.left - o.left, y: b.top - o.top, w: b.width, h: b.height };
    };
    setGeo({ w: o.width, h: o.height, p: rel(P), r: rel(R) });
  }, []);
  useEffect(() => {
    const W = wrap.current;
    if (!W || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(W);
    return () => ro.disconnect();
  }, [measure]);

  const { p, r } = geo ?? { p: null, r: null };
  const quad: Pt[] = r ? data.location.polygon.map(([x, y]) => [r.x + x * r.w, r.y + y * r.h]) : [];
  const corners: Pt[] = p
    ? [
        [p.x, p.y],
        [p.x + p.w, p.y],
        [p.x + p.w, p.y + p.h],
        [p.x, p.y + p.h],
      ]
    : [];
  return (
    <div className="aw-align">
      <div className="aw-pair-labels">
        <span>
          <Icon name="camera" />
          你的照片
        </span>
        <span>
          <Icon name="landmark" />
          知識庫原圖〈{title}〉
        </span>
      </div>
      <div className="aw-align__fig" ref={wrap}>
        <div className="aw-align__grid">
          <img ref={photo} src={photoUrl} alt="你的照片" onLoad={measure} />
          <img ref={ref} src={assetUrl(data.reference_url)} alt={`知識庫原圖〈${title}〉`} onLoad={measure} />
        </div>
        {geo && r && p && (
          <svg className="aw-align__svg" width={geo.w} height={geo.h} viewBox={`0 0 ${geo.w} ${geo.h}`} aria-hidden>
            <defs>
              <clipPath id={clip}>
                <rect x={r.x} y={r.y} width={r.w} height={r.h} />
              </clipPath>
              <mask id={mask}>
                <rect x={r.x} y={r.y} width={r.w} height={r.h} fill="#fff" />
                <polygon points={ptsAttr(quad)} fill="#000" />
              </mask>
            </defs>
            {cover && (
              <g clipPath={`url(#${clip})`}>
                <rect x={r.x} y={r.y} width={r.w} height={r.h} fill="#000" opacity={0.5} mask={`url(#${mask})`} />
                <polygon points={ptsAttr(quad)} className="aw-cover" />
              </g>
            )}
            {lines &&
              corners.map(([x1, y1], i) => (
                <g key={i} style={{ color: LINE_COLORS[i] }}>
                  <line x1={x1} y1={y1} x2={quad[i][0]} y2={quad[i][1]} stroke="currentColor" strokeWidth={1} opacity={0.75} />
                  <circle cx={x1} cy={y1} r={3.5} fill="none" stroke="currentColor" strokeWidth={1.5} />
                  <circle cx={quad[i][0]} cy={quad[i][1]} r={3.5} fill="none" stroke="currentColor" strokeWidth={1.5} />
                </g>
              ))}
          </svg>
        )}
      </div>
      <p className="aw-align__text">
        {locationText(data, aspect)}
        {regions.length > 0 ? `找到 ${regions.length} 處不一樣的地方，切到「拖曳比對」或「差異疊圖」看。` : data.diff ? diffSummary(data, 0) : ""}
      </p>
      <div className="aw-specs">
        <div>
          <b className="num">{data.inliers}</b>
          <span>對應點</span>
        </div>
        <div>
          <b className="num">{pct(data.location.coverage)}</b>
          <span>拍到原畫的範圍</span>
        </div>
        <div>
          <b className="num">{aspect ? `${Math.abs(rotationOf(data.location.polygon, aspect)).toFixed(1)}°` : "—"}</b>
          <span>旋轉角度</span>
        </div>
        <div>
          <b className="num">{data.latency_ms} ms</b>
          <span>對位耗時</span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 拖曳比對／差異疊圖

function Swipe({
  data,
  photoUrl,
  overlay,
  boxes,
  regions,
  active,
  onHover,
  onPin,
}: {
  data: ImageAlignment;
  photoUrl: string;
  overlay: boolean;
  boxes: boolean;
  regions: DiffRegion[];
  active: number | null;
  onHover: (i: number | null) => void;
  onPin: (i: number) => void;
}) {
  const [box, size] = useSize<HTMLDivElement>();
  const [pos, setPos] = useState(55);
  const [nat, setNat] = useState<{ w: number; h: number } | null>(null);
  const dragging = useRef(false);
  const mask = useId();
  const { w: W, h: H } = size;
  const quad = scalePts(data.location.polygon, W, H);
  const tf = nat && W > 0 ? quadTransform(nat.w, nat.h, quad) : null;

  const fromEvent = (e: ReactPointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width > 0) setPos(Math.max(0, Math.min(100, ((e.clientX - r.left) / r.width) * 100)));
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      setPos((p) => Math.max(0, Math.min(100, p + (e.key === "ArrowRight" ? 5 : -5))));
    }
  };

  return (
    <div
      ref={box}
      className={`aw-swipe${overlay ? " is-overlay" : ""}${boxes ? "" : " no-boxes"}`}
      role={overlay ? undefined : "slider"}
      aria-label={overlay ? undefined : "左右拖曳：左邊是你的照片（已對位），右邊是知識庫原圖"}
      aria-valuemin={overlay ? undefined : 0}
      aria-valuemax={overlay ? undefined : 100}
      aria-valuenow={overlay ? undefined : Math.round(pos)}
      tabIndex={overlay ? undefined : 0}
      onKeyDown={overlay ? undefined : onKey}
      onPointerDown={(e) => {
        if (overlay || (e.target as Element).closest(".aw-rg")) return;
        dragging.current = true;
        e.currentTarget.setPointerCapture?.(e.pointerId);
        fromEvent(e);
      }}
      onPointerMove={(e) => dragging.current && fromEvent(e)}
      onPointerUp={() => (dragging.current = false)}
      onPointerCancel={() => (dragging.current = false)}
    >
      <img className="aw-swipe__base" src={assetUrl(overlay ? data.overlay_url : data.reference_url)} alt={overlay ? "知識庫原圖上標出和照片不一樣的地方" : "知識庫原圖"} draggable={false} />
      {!overlay && (
        <div className="aw-swipe__top" style={{ clipPath: `inset(0 ${100 - pos}% 0 0)` }}>
          {W > 0 && (
            <svg className="aw-swipe__layer" viewBox={`0 0 ${W} ${H}`} aria-hidden>
              <defs>
                <mask id={mask}>
                  <rect width={W} height={H} fill="#fff" />
                  <polygon points={ptsAttr(quad)} fill="#000" />
                </mask>
              </defs>
              <rect width={W} height={H} fill="#000" opacity={0.5} mask={`url(#${mask})`} />
            </svg>
          )}
          <img
            className="aw-swipe__photo"
            src={photoUrl}
            alt="你的照片，依對位結果投影回原圖座標"
            draggable={false}
            onLoad={(e) => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
            style={nat && tf ? { width: nat.w, height: nat.h, transform: tf } : { visibility: "hidden" }}
          />
        </div>
      )}
      {W > 0 && (
        <svg className="aw-swipe__layer aw-swipe__marks" viewBox={`0 0 ${W} ${H}`}>
          <polygon points={ptsAttr(quad)} className="aw-cover" />
          {regions.map((r, i) => {
            const [x0, y0, x1, y1] = [r.bbox[0] * W, r.bbox[1] * H, r.bbox[2] * W, r.bbox[3] * H];
            const cls = DIFF_KIND[r.kind].cls;
            return (
              <g
                key={r.bbox.join(",")}
                className={`aw-rg${active === i ? " is-hl" : active != null ? " is-dim" : ""}`}
                onMouseEnter={() => onHover(i)}
                onMouseLeave={() => onHover(null)}
                onClick={() => onPin(i)}
              >
                <rect className={`aw-rfill ${cls}`} x={x0} y={y0} width={x1 - x0} height={y1 - y0} rx={2} />
                <rect className={`aw-rbox ${cls}`} x={x0} y={y0} width={x1 - x0} height={y1 - y0} rx={2} />
                {!overlay && (
                  <>
                    <circle className={`aw-rbadge ${cls}`} cx={x0} cy={y0} r={9} />
                    <text className="aw-rlabel" x={x0} y={y0 + 4} textAnchor="middle">
                      {i + 1}
                    </text>
                  </>
                )}
              </g>
            );
          })}
        </svg>
      )}
      {!overlay && <div className="aw-swipe__handle" style={{ left: `${pos}%` }} />}
      <span className="aw-swipe__tag aw-swipe__tag--l">{overlay ? "知識庫原圖＋差異疊圖" : "你的照片（已對位）"}</span>
      {!overlay && <span className="aw-swipe__tag aw-swipe__tag--r">知識庫原圖</span>}
    </div>
  );
}

// ---------------------------------------------------------------- 匯出報告（docs/adr/017：瀏覽器裡產生、另外記一筆稽核）

async function compareReport({ title, data, regions, photoUrl, who }: { title: string; data: ImageAlignment; regions: DiffRegion[]; photoUrl: string; who: string }) {
  const [photo, overlay] = await Promise.all([imageDataUrl(photoUrl), imageDataUrl(assetUrl(data.overlay_url))]);
  const img = (src: string | null, alt: string) => (src ? `<img class="photo" src="${src}" alt="${esc(alt)}" style="max-width:380px;max-height:380px">` : "");
  const rows = regions
    .map((r, i) => `<tr><td>${i + 1}</td><td>${esc(DIFF_KIND[r.kind].label)}</td><td>畫面${esc(whereOnPainting(centerOf(r.bbox)))}</td><td>${esc(pct(r.area_ratio))}</td></tr>`)
    .join("");
  const body = `
<h2>照片與知識庫原圖</h2>
<div class="row">${img(photo, "你的照片")}${img(overlay, "知識庫原圖＋差異疊圖")}</div>
<p>${esc(locationText(data, null))}對應點 ${data.inliers} 個。</p>
<h2>不一樣的地方</h2>
<p>${esc(diffSummary(data, regions.length))}</p>
${regions.length ? `<table><tr><th>編號</th><th>種類</th><th>位置</th><th>占比對範圍</th></tr>${rows}</table>` : ""}
${data.notes.map((n) => `<p class="fine">${esc(n)}</p>`).join("")}
<p class="fine">ORB 特徵點＋RANSAC 對位、形狀與顏色比對，系統計算，不經過生成模型。差異是候選，請對照原圖確認；照片的顏色受現場燈光影響。</p>`;
  return reportPage({ title: `〈${title}〉照片比對報告`, subtitle: "藝術模組・影像對位與差異", level: "公開", who, body });
}
