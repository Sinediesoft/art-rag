import { useState } from "react";
import { api, assetUrl, type ColorAnalysis } from "../../api/client";
import { useArtworkColors, usePhotoColors } from "../../api/hooks";
import { Spinner } from "../shell/Icons";

/**
 * 藝術模組「色溫」（docs/adr/010 的色彩分析）：後端沒有算色溫（Kelvin），只有冷暖占比，
 * 所以畫成冷↔暖的漸層軸，標出原圖（有附照片時再加照片）落在哪裡；下面是主色、冷暖、明度、彩度。
 * 數字全部來自 /artworks/{id}/colors、/images/{id}/colors，系統計算，不經過生成模型。
 */

/** 冷暖軸上的位置（0＝全冷、100＝全暖）：暖色占比減冷色占比 */
const tempPos = (c: ColorAnalysis) => Math.max(2, Math.min(98, 50 + (c.temperature.warm - c.temperature.cool) * 50));
const pc = (x: number) => `${Math.round(x * 100)}%`;

// 比例條的資料色：冷暖和 ColorAnalysisCard.tsx 一樣；明度、彩度照原型（mockups/painting-module.html）的暗色展牆配色
const TEMP = [
  ["warm", "暖色", "var(--aw-warm)"],
  ["neutral", "中性色", "var(--aw-neutral)"],
  ["cool", "冷色", "var(--aw-cool)"],
] as const;
const LIGHT = [
  ["dark", "暗調", "#4a4035"],
  ["mid", "中間調", "#8e8270"],
  ["light", "亮調", "#d9cfbd"],
] as const;
const CHROMA = [
  ["low", "低彩度", "#6f675c"],
  ["mid", "中彩度", "#a8865e"],
  ["high", "高彩度", "#c9693f"],
] as const;

export function ColorSection({ artworkId, imageId, thumb, focus }: { artworkId: string; imageId?: string; thumb: string; focus?: number }) {
  const orig = useArtworkColors(artworkId);
  const photo = usePhotoColors(imageId ?? null);
  const o = orig.data;
  const p = photo.data;
  return (
    <section className={`aw-sec${focus ? " is-flash" : ""}`} id="aw-sec-color" key={focus}>
      <div className="aw-sec__head">
        <p className="aw-sec__kind">
          <span className="aw-dot" />
          <b>色溫</b>
          <span className="aw-en">Color</span>
        </p>
      </div>
      <div className="aw-card">
        {orig.isLoading && (
          <p className="aw-pending">
            <Spinner />
            讀取色彩分析…
          </p>
        )}
        {orig.error && <p className="aw-empty">這幅畫沒有色彩分析（{(orig.error as Error).message}）。</p>}
        {o && (
          <>
            <p className="aw-temp__sum">{o.summary}</p>
            <div className="aw-temp">
              <div className="aw-temp__grad" />
              <div className="aw-temp__mk" style={{ left: `${tempPos(o)}%` }}>
                <i />
                <span>原圖</span>
              </div>
              {p && (
                <div className="aw-temp__mk is-photo" style={{ left: `${tempPos(p)}%` }}>
                  <i />
                  <span>你的照片</span>
                </div>
              )}
              <div className="aw-temp__ends">
                <span>冷</span>
                <span>中性</span>
                <span>暖</span>
              </div>
            </div>

            <div className={`aw-temp__grid${imageId ? "" : " is-single"}`}>
              <Palette head="知識庫原圖・主色" thumb={assetUrl(thumb)} data={o} />
              {imageId && (p ? <Palette head="你的照片・主色" thumb={api.uploadedImageUrl(imageId)} data={p} /> : photo.isLoading ? <p className="aw-pending"><Spinner />分析照片的顏色…</p> : null)}
            </div>

            <div className="aw-shares">
              <div className="aw-share">
                <p className="aw-share__head">
                  <b>冷暖</b>
                  {p && <span className="aw-label">原圖 ／ 照片</span>}
                </p>
                <Bar label={p ? "原圖" : undefined} parts={TEMP.map(([k, , c]) => [o.temperature[k], c])} />
                {p && <Bar label="照片" parts={TEMP.map(([k, , c]) => [p.temperature[k], c])} />}
                <Legend items={TEMP.map(([k, l, c]) => [p ? l : `${l} ${pc(o.temperature[k])}`, c])} />
              </div>
              <div className="aw-temp__grid">
                <div className="aw-share">
                  <p className="aw-share__head">
                    <b>明度（平均 L* {o.lightness.mean.toFixed(1)}）</b>
                  </p>
                  <Bar parts={LIGHT.map(([k, , c]) => [o.lightness[k], c])} />
                  <Legend items={LIGHT.map(([k, l, c]) => [`${l} ${pc(o.lightness[k])}`, c])} />
                  <Hist values={o.lightness.histogram} />
                  <p className="aw-hist__cap">L* 分布</p>
                </div>
                <div className="aw-share">
                  <p className="aw-share__head">
                    <b>彩度（中位數 C* {o.chroma.median.toFixed(1)}）</b>
                  </p>
                  <Bar parts={CHROMA.map(([k, , c]) => [o.chroma[k], c])} />
                  <Legend items={CHROMA.map(([k, l, c]) => [`${l} ${pc(o.chroma[k])}`, c])} />
                  <Hist values={o.chroma.histogram} />
                  <p className="aw-hist__cap">C* 分布</p>
                </div>
              </div>
            </div>

            <details className="aw-nums">
              <summary>數值表</summary>
              <table className="aw-table">
                <thead>
                  <tr>
                    <th>項目</th>
                    <th>知識庫原圖</th>
                    {p && <th>你的照片</th>}
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ["主色 1", (c: ColorAnalysis) => (c.palette[0] ? `${c.palette[0].name} ${c.palette[0].hex.toUpperCase()}・${pc(c.palette[0].share)}` : "—")],
                      ["冷暖", (c: ColorAnalysis) => `暖 ${pc(c.temperature.warm)}・中性 ${pc(c.temperature.neutral)}・冷 ${pc(c.temperature.cool)}`],
                      ["明度", (c: ColorAnalysis) => `平均 L* ${c.lightness.mean.toFixed(1)}・P5–P95 ${c.lightness.p5.toFixed(1)}–${c.lightness.p95.toFixed(1)}`],
                      ["彩度", (c: ColorAnalysis) => `中位數 C* ${c.chroma.median.toFixed(1)}`],
                    ] as const
                  ).map(([k, f]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td>{f(o)}</td>
                      {p && <td>{f(p)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
            <p className="aw-credit">
              {p && (
                <>
                  ※ 照片的顏色受現場燈光影響，冷暖只能當參考。
                  <br />
                </>
              )}
              {o.notes.map((n) => (
                <span key={n}>
                  {n}
                  <br />
                </span>
              ))}
              依數位原圖計算・{o.method}・{o.latency_ms} ms・系統計算，不經過生成模型
            </p>
          </>
        )}
      </div>
    </section>
  );
}

/** 主色：比例條＋清單，點一個色塊只亮那一色，再點取消 */
function Palette({ head, thumb, data }: { head: string; thumb: string; data: ColorAnalysis }) {
  const [on, setOn] = useState<number | null>(null);
  const pick = (i: number) => setOn((x) => (x === i ? null : i));
  const colors = data.palette.slice(0, 8);
  return (
    <div>
      <p className="aw-pal__head">
        <img src={thumb} alt="" />
        {head}
      </p>
      <div className={`aw-pal__strip${on != null ? " is-focus" : ""}`}>
        {colors.map((c, i) => (
          <button key={c.hex + i} type="button" className={on === i ? "is-on" : ""} style={{ width: `${c.share * 100}%`, background: c.hex }} onClick={() => pick(i)} title={`${c.name} ${c.hex} ${pc(c.share)}`} aria-label={`${c.name} ${pc(c.share)}`} />
        ))}
      </div>
      <ul className="aw-pal__list">
        {colors.map((c, i) => (
          <li key={c.hex + i}>
            <button type="button" className={on === i ? "is-on" : ""} onClick={() => pick(i)}>
              <span className="aw-pal__sw" style={{ background: c.hex }} />
              {c.name}
              <span className="aw-pal__hex">{c.hex.toUpperCase()}</span>
              <span className="aw-pal__pct num">{pc(c.share)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Bar({ label, parts }: { label?: string; parts: (readonly [number, string])[] }) {
  const bar = (
    <div className="aw-sbar">
      {parts.map(([v, c], i) => (
        <i key={i} style={{ width: `${v * 100}%`, background: c }} />
      ))}
    </div>
  );
  if (!label) return bar;
  return (
    <div className="aw-bar-row">
      <span>{label}</span>
      {bar}
    </div>
  );
}

function Legend({ items }: { items: (readonly [string, string])[] }) {
  return (
    <p className="aw-legend">
      {items.map(([l, c]) => (
        <span key={l}>
          <i style={{ background: c }} />
          {l}
        </span>
      ))}
    </p>
  );
}

function Hist({ values }: { values: number[] }) {
  const max = Math.max(...values, 1e-9);
  return (
    <div className="aw-hist" aria-hidden>
      {values.map((v, i) => (
        <i key={i} style={{ height: `${(v / max) * 100}%` }} />
      ))}
    </div>
  );
}
