import { useEffect, useMemo, useRef, useState } from "react";
import { assetUrl, type ApiError, type ColorAnalysis } from "../../api/client";
import { useArtworkColors, usePhotoColors } from "../../api/hooks";
import { ErrorMessage, Loading } from "../common/Feedback";

type Segment = { key: string; label: string; color: string; value: number };

// 比例條配色（docs/adr/010）：已用 dataviz 的 validate_palette.js 對舊卡片底色 #fffdf8 驗過（現在卡片底色 #ffffff，差異極小）。
// 冷暖：暖、冷兩極＋中性灰居中；明度：暗→亮單一色相；彩度：灰→鮮單一色相
const TEMPERATURE = [
  ["warm", "暖色", "#eb6834"],
  ["neutral", "中性色", "#a8a49b"],
  ["cool", "冷色", "#2a78d6"],
] as const;
const LIGHTNESS = [
  ["dark", "暗調", "#2f2c28"],
  ["mid", "中間調", "#77726a"],
  ["light", "亮調", "#b0aa9e"],
] as const;
const CHROMA = [
  ["low", "低彩度", "#c4ad9e"],
  ["mid", "中彩度", "#d67a48"],
  ["high", "高彩度", "#a33c13"],
] as const;
const CARD_BG = [255, 255, 255]; // --color-card：沒選到的色塊混向卡片底色

const pct = (v: number) => `${Math.round(v * 100)}%`;

function segments(
  spec: readonly (readonly [string, string, string])[],
  values: Record<string, number>,
): Segment[] {
  return spec.map(([key, label, color]) => ({ key, label, color, value: values[key] ?? 0 }));
}

/** 三段比例條：每段旁邊直接標百分比（不靠 tooltip），滑過時其他段變淡 */
function ShareBar({ title, items }: { title: string; items: Segment[] }) {
  const [hover, setHover] = useState<string | null>(null);
  return (
    <div>
      <p className="mb-1 text-xs font-semibold text-ink-80">{title}</p>
      <div className="flex h-2.5 gap-[2px]" aria-hidden>
        {items
          .filter((s) => s.value > 0)
          .map((s) => (
            <span
              key={s.key}
              onPointerEnter={() => setHover(s.key)}
              onPointerLeave={() => setHover(null)}
              className="h-full min-w-[2px] transition-opacity first:rounded-l-[4px] last:rounded-r-[4px]"
              style={{
                flex: `${s.value} 1 0`,
                background: s.color,
                opacity: hover && hover !== s.key ? 0.35 : 1,
              }}
            />
          ))}
      </div>
      <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-ink-80">
        {items.map((s) => (
          <li key={s.key} className={`flex items-center gap-1 ${hover === s.key ? "font-semibold text-ink" : ""}`}>
            <span className="h-2 w-2 rounded-[2px]" style={{ background: s.color }} />
            {s.label}
            <span className="tabular-nums text-ink">{pct(s.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 10 格長條圖（單一資料系列）：整欄都是滑鼠感應區，數值顯示在下方 */
function Histogram({ bins, axis }: { bins: number[]; axis: string }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...bins) || 1;
  return (
    <figure className="mt-2">
      <div className="flex h-10 items-end gap-[2px]">
        {bins.map((b, i) => (
          <div
            key={i}
            className="flex h-full flex-1 items-end"
            onPointerEnter={() => setHover(i)}
            onPointerLeave={() => setHover(null)}
          >
            <span
              className={`w-full rounded-t-[4px] ${hover === i ? "bg-ink" : "bg-ink-80/60"}`}
              style={{ height: b > 0 ? `${Math.max(8, (b / max) * 100)}%` : 0 }}
            />
          </div>
        ))}
      </div>
      <figcaption className="mt-0.5 flex justify-between text-[10px] tabular-nums text-ink-48">
        <span>0</span>
        <span>{hover === null ? `${axis} 分布` : `${axis} ${hover * 10}–${hover * 10 + 10}：${pct(bins[hover])}`}</span>
        <span>100</span>
      </figcaption>
    </figure>
  );
}

function nearest(r: number, g: number, b: number, palette: number[][]): number {
  let best = 0;
  let bestD = Infinity;
  palette.forEach(([pr, pg, pb], i) => {
    const d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2;
    if (d < bestD) [best, bestD] = [i, d];
  });
  return best;
}

/** 色塊分布圖：fetch 成 blob 再畫進 canvas（同源，canvas 才讀得到像素）；選了某色就把其他色混向底色 */
function ColorMap({ src, palette, selected }: { src: string; palette: number[][]; selected: number | null }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [bitmap, setBitmap] = useState<ImageBitmap | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setBitmap(null);
    setFailed(false);
    fetch(src)
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.blob();
      })
      .then((blob) => createImageBitmap(blob))
      .then((b) => alive && setBitmap(b))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [src]);

  useEffect(() => {
    const c = canvas.current;
    if (!c || !bitmap) return;
    c.width = bitmap.width;
    c.height = bitmap.height;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    if (!ctx) return;
    ctx.drawImage(bitmap, 0, 0);
    if (selected === null) return;
    const img = ctx.getImageData(0, 0, c.width, c.height);
    const px = img.data;
    for (let i = 0; i < px.length; i += 4) {
      if (nearest(px[i], px[i + 1], px[i + 2], palette) === selected) continue;
      for (let k = 0; k < 3; k++) px[i + k] = CARD_BG[k] + (px[i + k] - CARD_BG[k]) * 0.12;
    }
    ctx.putImageData(img, 0, 0);
  }, [bitmap, selected, palette]);

  if (failed) return <p className="text-xs text-ink-48">色塊分布圖載入失敗</p>;
  // 圖還沒載好時先留一塊空間（canvas 沒設尺寸會是 300×150，載好後卡片高度會跳一下）；
  // 還不知道直幅或橫幅，高度取橫幅（約 300 px）與直幅（512 px）之間，且不超過 70vh
  if (!bitmap)
    return (
      <div
        role="status"
        className="flex h-[min(24rem,70vh)] items-center justify-center rounded-lg border border-hairline bg-parchment-deep text-xs text-ink-48"
      >
        載入色塊分布圖…
      </div>
    );
  // 保持長寬比、不超過卡片寬度、最高 70vh（同畫作頁左邊的原圖）；
  // w-auto／h-auto 讓 canvas 以自己的像素尺寸（長邊 512）顯示，不會被卡片寬度放大成又糊又高的圖
  return (
    <canvas
      ref={canvas}
      role="img"
      aria-label="色塊分布圖：每個像素塗成所屬的主色"
      className="mx-auto block h-auto max-h-[70vh] w-auto max-w-full rounded-lg border border-hairline bg-parchment-deep"
    />
  );
}

export function ColorAnalysisCard({ data, title, hint }: { data: ColorAnalysis; title: string; hint?: string }) {
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const palette = useMemo(() => data.palette.map((p) => p.rgb), [data]);
  const focus = hover ?? selected;
  const toggle = (i: number) => setSelected(selected === i ? null : i);
  const { temperature: t, lightness: l, chroma: c } = data;

  return (
    <section className="card flex flex-col gap-4 p-5">
      <header>
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-sm text-ink-80">{data.summary}</p>
        {hint && <p className="mt-1 text-xs text-ink-48">{hint}</p>}
      </header>

      <div>
        <div className="flex h-8 gap-[2px]">
          {data.palette.map((p, i) => (
            <button
              key={`${p.hex}-${i}`}
              type="button"
              onClick={() => toggle(i)}
              onPointerEnter={() => setHover(i)}
              onPointerLeave={() => setHover(null)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              aria-pressed={selected === i}
              aria-label={`${p.name} ${p.hex} ${pct(p.share)}：在色塊分布圖只顯示這一色`}
              className={`h-full min-w-[6px] transition first:rounded-l-[4px] last:rounded-r-[4px] ${
                focus !== null && focus !== i ? "opacity-40" : ""
              } ${selected === i ? "ring-2 ring-accent-focus ring-offset-1 ring-offset-card" : ""}`}
              style={{ flex: `${p.share} 1 0`, background: p.hex }}
            />
          ))}
        </div>
        <ul className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-3">
          {data.palette.map((p, i) => (
            <li key={`${p.hex}-${i}`}>
              <button
                type="button"
                onClick={() => toggle(i)}
                onPointerEnter={() => setHover(i)}
                onPointerLeave={() => setHover(null)}
                aria-pressed={selected === i}
                className={`flex w-full items-center gap-2 rounded-md px-1 py-0.5 text-left text-xs transition hover:bg-parchment-deep ${
                  selected === i ? "bg-accent-soft" : ""
                }`}
              >
                <span className="h-4 w-4 shrink-0 rounded-[4px] border border-ink/10" style={{ background: p.hex }} />
                <span className="font-semibold text-ink">{p.name}</span>
                <span className="font-mono text-ink-48">{p.hex}</span>
                <span className="ml-auto tabular-nums text-ink-80">{pct(p.share)}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div>
        <ColorMap src={assetUrl(data.map_url)} palette={palette} selected={selected} />
        <p className="mt-1 text-xs text-ink-48">
          {selected === null
            ? "點色盤上的顏色，色塊分布圖就只亮那一色"
            : `只顯示「${data.palette[selected].name} ${data.palette[selected].hex}」，再點一次取消`}
        </p>
      </div>

      <ShareBar title="冷暖" items={segments(TEMPERATURE, { warm: t.warm, neutral: t.neutral, cool: t.cool })} />
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <ShareBar
            title={`明度（平均 L* ${l.mean}）`}
            items={segments(LIGHTNESS, { dark: l.dark, mid: l.mid, light: l.light })}
          />
          <Histogram bins={l.histogram} axis="L*" />
        </div>
        <div>
          <ShareBar
            title={`彩度（中位數 C* ${c.median}）`}
            items={segments(CHROMA, { low: c.low, mid: c.mid, high: c.high })}
          />
          <Histogram bins={c.histogram} axis="C*" />
        </div>
      </div>

      <details className="text-xs text-ink-80">
        <summary className="cursor-pointer text-accent">數值表</summary>
        <table className="mt-2 w-full text-left tabular-nums">
          <tbody>
            {data.palette.map((p, i) => (
              <tr key={`${p.hex}-${i}`} className="border-t border-hairline">
                <th className="py-1 pr-2 font-normal text-ink-48">主色 {i + 1}</th>
                <td>
                  {p.name} {p.hex} · {pct(p.share)} · L* {p.lab[0]}
                </td>
              </tr>
            ))}
            <tr className="border-t border-hairline">
              <th className="py-1 pr-2 font-normal text-ink-48">冷暖</th>
              <td>
                暖 {pct(t.warm)} · 冷 {pct(t.cool)} · 中性 {pct(t.neutral)}
              </td>
            </tr>
            <tr className="border-t border-hairline">
              <th className="py-1 pr-2 font-normal text-ink-48">明度</th>
              <td>
                暗 {pct(l.dark)} · 中 {pct(l.mid)} · 亮 {pct(l.light)} · 平均 {l.mean} · P5–P95 {l.p5}–{l.p95}
              </td>
            </tr>
            <tr className="border-t border-hairline">
              <th className="py-1 pr-2 font-normal text-ink-48">彩度</th>
              <td>
                低 {pct(c.low)} · 中 {pct(c.mid)} · 高 {pct(c.high)} · 中位數 {c.median}
              </td>
            </tr>
            <tr className="border-t border-hairline">
              <th className="py-1 pr-2 font-normal text-ink-48">L* 分布</th>
              <td>{l.histogram.map(pct).join(" / ")}</td>
            </tr>
            <tr className="border-t border-hairline">
              <th className="py-1 pr-2 font-normal text-ink-48">C* 分布</th>
              <td>{c.histogram.map(pct).join(" / ")}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <ul className="flex flex-col gap-0.5 text-xs text-ink-48">
        {data.notes.map((n) => (
          <li key={n}>※ {n}</li>
        ))}
      </ul>
      <p className="text-[11px] text-ink-48">
        {data.source === "original" ? "依數位原圖" : "依你的照片"}計算 · CIELAB k-means（{data.method}）
        {data.latency_ms > 0 && ` · ${data.latency_ms} ms`} · 系統計算，不經過生成模型
      </p>
    </section>
  );
}

export function ArtworkColors({ artworkId }: { artworkId: string }) {
  const { data, isLoading, error } = useArtworkColors(artworkId);
  if (isLoading) return <Loading label="載入色彩分析…" />;
  if (error || !data)
    return (
      <ErrorMessage
        title="色彩分析載入失敗"
        message={(error as Error)?.message ?? ""}
        requestId={(error as ApiError)?.requestId}
      />
    );
  // key：換畫作就重掛載，點選與滑過的狀態不會帶到下一幅（色票數不同時 palette[selected] 會是 undefined）
  return <ColorAnalysisCard key={artworkId} data={data} title="色彩分析" />;
}

export function PhotoColors({ imageId, matched }: { imageId: string; matched: boolean }) {
  const { data, isLoading, error } = usePhotoColors(imageId);
  if (isLoading) return <Loading label="分析照片的色彩…" />;
  if (error || !data)
    return (
      <ErrorMessage
        title="色彩分析失敗"
        message={(error as Error)?.message ?? ""}
        requestId={(error as ApiError)?.requestId}
      />
    );
  return (
    <ColorAnalysisCard
      key={imageId}
      data={data}
      title="色彩分析（依你的照片）"
      hint={
        matched
          ? "照片會受光線與白平衡影響；原圖的分析見「畫作介紹」。"
          : "知識庫中沒有這幅畫，但照片的色彩仍然可以分析。"
      }
    />
  );
}
