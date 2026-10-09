// 圈區域、寫解說（docs/adr/030 第 2 步）：藝術家在畫上拖出矩形或自由圈，填名稱與解說，送出給主管收錄。
// 座標和顯示用的 RegionOverlay 同一套（相對原圖的 0–1）；自由圈的軌跡簡化到 64 點以內再送（後端上限）。
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type PointerEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, assetUrl, type Account, type ArtworkDetail, type RegionDraft } from "../../api/client";
import { RegionOverlay, regionWhere } from "./RegionOverlay";

type Pt = [number, number];
type Tool = "rect" | "free";

const MAX_POINTS = 64;
const MIN_AREA = 1e-4; // 和後端 kb.py 的 _MIN_REGION_AREA 相同
const clamp = (v: number) => Math.min(1, Math.max(0, v));
const round4 = (v: number) => Math.round(v * 10_000) / 10_000;

function area(points: Pt[]): number {
  let s = 0;
  points.forEach(([x, y], i) => {
    const [nx, ny] = points[(i + 1) % points.length];
    s += x * ny - nx * y;
  });
  return Math.abs(s) / 2;
}

function rectOf([ax, ay]: Pt, [bx, by]: Pt): Pt[] {
  const [x0, x1, y0, y1] = [Math.min(ax, bx), Math.max(ax, bx), Math.min(ay, by), Math.max(ay, by)];
  return [
    [x0, y0],
    [x1, y0],
    [x1, y1],
    [x0, y1],
  ];
}

function segDist([px, py]: Pt, [ax, ay]: Pt, [bx, by]: Pt): number {
  const [dx, dy] = [bx - ax, by - ay];
  const len2 = dx * dx + dy * dy;
  const t = len2 ? clamp(((px - ax) * dx + (py - ay) * dy) / len2) : 0;
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}

/** Douglas–Peucker：離首尾連線最遠的點超過 eps 就保留、兩邊各自再簡化 */
function dp(pts: Pt[], eps: number): Pt[] {
  if (pts.length < 3) return pts;
  let idx = -1;
  let dmax = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const d = segDist(pts[i], pts[0], pts[pts.length - 1]);
    if (d > dmax) [dmax, idx] = [d, i];
  }
  if (dmax <= eps) return [pts[0], pts[pts.length - 1]];
  return [...dp(pts.slice(0, idx + 1), eps).slice(0, -1), ...dp(pts.slice(idx), eps)];
}

/** 自由圈的軌跡（幾百個點）簡化到 64 點以內；終點貼著起點時拿掉（多邊形本來就會自己閉合） */
function simplify(trail: Pt[]): Pt[] {
  let eps = 0.002;
  let out = dp(trail, eps);
  while (out.length > MAX_POINTS) {
    eps *= 1.5;
    out = dp(trail, eps);
  }
  if (out.length > 3 && Math.hypot(out[0][0] - out.at(-1)![0], out[0][1] - out.at(-1)![1]) < 0.01) out = out.slice(0, -1);
  return out.map(([x, y]) => [round4(x), round4(y)]);
}

const field = "w-full rounded-lg border border-hairline bg-white px-2 py-1.5 text-sm text-ink outline-none focus:border-accent";

/** 畫作頁的「圈一塊、寫解說」：畫上拖曳圈區域，下面填表單；送出就是草稿，等主管收錄 */
export function RegionEditor({ artwork, me, onClose }: { artwork: ArtworkDetail; me: Account; onClose: () => void }) {
  const qc = useQueryClient();
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ start: Pt; trail: Pt[] } | null>(null);
  const [tool, setTool] = useState<Tool>("rect");
  const [points, setPoints] = useState<Pt[]>([]);
  const [label, setLabel] = useState("");
  const [text, setText] = useState("");
  const [license, setLicense] = useState<"CC BY 4.0" | "CC0">("CC BY 4.0");
  const [attribution, setAttribution] = useState(me.label);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<RegionDraft | null>(null);

  const at = (e: PointerEvent<SVGSVGElement>): Pt => {
    const r = svgRef.current!.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width), clamp((e.clientY - r.top) / r.height)];
  };
  const down = (e: PointerEvent<SVGSVGElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = at(e);
    drag.current = { start: p, trail: [p] };
    setPoints([p]);
  };
  const move = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const p = at(e);
    if (tool === "rect") return setPoints(rectOf(d.start, p));
    const last = d.trail[d.trail.length - 1];
    if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 0.003) {
      d.trail.push(p);
      setPoints([...d.trail]);
    }
  };
  const up = () => {
    const d = drag.current;
    drag.current = null;
    if (d && tool === "free") setPoints(simplify(d.trail));
    if (d && tool === "rect") setPoints((pts) => pts.map(([x, y]) => [round4(x), round4(y)]));
  };

  const shapeOk = points.length >= 3 && area(points) >= MIN_AREA;
  const textLen = text.trim().length;
  const ready = shapeOk && label.trim() && textLen >= 20 && (license === "CC0" || attribution.trim());

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const d = await api.createRegionDraft(artwork.id, {
        label: label.trim(),
        points,
        text: text.trim(),
        license,
        attribution: license === "CC BY 4.0" ? attribution.trim() : null,
      });
      setSent(d);
      void qc.invalidateQueries({ queryKey: ["region-drafts"] });
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const again = () => {
    setSent(null);
    setPoints([]);
    setLabel("");
    setText("");
  };

  const existing = artwork.regions?.items ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-center rounded-2xl bg-parchment-deep p-6 sm:p-10">
        <div className="relative w-fit max-w-full select-none">
          <img
            src={assetUrl(artwork.image_url)}
            alt={artwork.title.zh}
            draggable={false}
            className="product-shadow block max-h-[64vh] w-auto max-w-full"
          />
          {existing.length > 0 && <RegionOverlay regions={existing} />}
          <svg
            ref={svgRef}
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            className={`absolute inset-0 h-full w-full touch-none ${sent ? "pointer-events-none" : "cursor-crosshair"}`}
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={up}
            onPointerCancel={up}
            aria-label="在畫上拖曳，圈出要解說的區域"
          >
            {points.length > 1 && (
              <polygon
                points={points.map(([x, y]) => `${x},${y}`).join(" ")}
                fill="rgb(255 159 10 / 0.18)"
                stroke="var(--color-warning-on-dark)"
                strokeWidth={3}
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
              />
            )}
          </svg>
        </div>
      </div>

      {sent ? (
        <div className="card flex flex-col gap-3 p-5">
          <p className="font-semibold text-success">已送出「{sent.label}」，等主管收錄。</p>
          <p className="text-sm text-ink-80">
            主管收錄後會寫進知識庫、重建索引，這一塊就會出現在畫上，問答也會引用。進度在{" "}
            <Link to="/approvals" className="link">
              核准紀錄
            </Link>
            。
          </p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn-ghost px-4 py-1.5 text-sm" onClick={again}>
              再圈一塊
            </button>
            <button type="button" className="btn-primary px-4 py-1.5 text-sm" onClick={onClose}>
              完成
            </button>
          </div>
        </div>
      ) : (
        <div className="card flex flex-col gap-3 p-5">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-ink-48">圈法</span>
            {(
              [
                ["rect", "拖出矩形"],
                ["free", "自由圈"],
              ] as const
            ).map(([t, name]) => (
              <button
                key={t}
                type="button"
                aria-pressed={tool === t}
                onClick={() => {
                  setTool(t);
                  setPoints([]);
                }}
                className={`rounded-full px-3 py-0.5 ${tool === t ? "bg-accent text-white" : "bg-parchment-deep text-ink-80"}`}
              >
                {name}
              </button>
            ))}
            <span className="text-ink-48">
              {shapeOk
                ? `已圈在畫面${regionWhere(points)}`
                : points.length > 1
                  ? "範圍太小，請圈大一點"
                  : "在畫上按住拖曳"}
            </span>
            {points.length > 0 && (
              <button type="button" className="link ml-auto" onClick={() => setPoints([])}>
                重畫
              </button>
            )}
          </div>

          <label className="flex flex-col gap-1 text-sm">
            這一塊叫什麼
            <input
              value={label}
              maxLength={30}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="例如：山頂的灌木叢"
              className={field}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            解說
            <textarea
              value={text}
              rows={5}
              maxLength={1000}
              onChange={(e) => setText(e.target.value)}
              placeholder="這一塊在畫什麼、為什麼這樣畫、背後的故事…"
              className={field}
            />
            <span className={`text-xs ${textLen >= 20 ? "text-ink-48" : "text-warning"}`}>
              {textLen} 字（至少 20 字）
            </span>
          </label>
          <fieldset className="flex flex-col gap-1.5 text-sm">
            <legend className="mb-1">授權（觀眾與問答會引用這段解說）</legend>
            <label className="flex items-center gap-2">
              <input type="radio" checked={license === "CC BY 4.0"} onChange={() => setLicense("CC BY 4.0")} />
              CC BY 4.0：引用時要標出署名
            </label>
            {license === "CC BY 4.0" && (
              <input
                value={attribution}
                maxLength={60}
                onChange={(e) => setAttribution(e.target.value)}
                placeholder="署名"
                aria-label="署名"
                className={`${field} ml-6 w-auto`}
              />
            )}
            <label className="flex items-center gap-2">
              <input type="radio" checked={license === "CC0"} onChange={() => setLicense("CC0")} />
              CC0：放棄權利，不用署名
            </label>
          </fieldset>

          {error && <p className="rounded-lg bg-danger-soft px-3 py-2 text-sm text-danger">{error}</p>}
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn-primary px-4 py-1.5 text-sm" disabled={!ready || busy} onClick={submit}>
              {busy ? "送出中…" : "送出給主管收錄"}
            </button>
            <button type="button" className="btn-ghost px-4 py-1.5 text-sm" onClick={onClose}>
              取消
            </button>
            <span className="text-xs text-ink-48">送出前會檢查解說裡有沒有要 AI 執行的指令</span>
          </div>
        </div>
      )}
    </div>
  );
}
