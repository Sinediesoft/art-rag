// 畫面區域（docs/adr/029）：在畫作上圈出的區域，座標是相對畫作原圖的 0–1（x 往右、y 往下）。
// 畫作頁在大圖上標出所有區域；問答的參考來源在縮圖上標出那一段講的是哪一塊。
import { useId } from "react";
import { assetUrl } from "../../api/client";
import { whereOnPainting } from "../../lib/format";

export interface RegionShape {
  id: string;
  label: string;
  points: number[][];
}

const toPoints = (points: number[][]) => points.map(([x, y]) => `${x},${y}`).join(" ");

/** 區域的外框中心落在畫面九宮格的哪一格，例如「右下」（和後端段落前面加的方位詞相同） */
export function regionWhere(points: number[][]): string {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return whereOnPainting([(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2]);
}

/** 疊在圖上的 SVG（父層要 relative、大小和圖一樣）。有 active 時那一塊以外調暗；給 onSelect 才能點區域 */
export function RegionOverlay({
  regions,
  active = null,
  onSelect,
}: {
  regions: RegionShape[];
  active?: string | null;
  onSelect?: (id: string) => void;
}) {
  const maskId = useId();
  const act = regions.find((r) => r.id === active);
  return (
    <svg
      viewBox="0 0 1 1"
      preserveAspectRatio="none"
      className={`absolute inset-0 h-full w-full ${onSelect ? "" : "pointer-events-none"}`}
      aria-hidden
    >
      {act && (
        <>
          <defs>
            <mask id={maskId}>
              <rect width="1" height="1" fill="white" />
              <polygon points={toPoints(act.points)} fill="black" />
            </mask>
          </defs>
          <rect
            width="1"
            height="1"
            fill="rgb(29 29 31 / 0.55)"
            mask={`url(#${maskId})`}
            className="pointer-events-none"
          />
        </>
      )}
      {regions.map((r) => (
        <polygon
          key={r.id}
          points={toPoints(r.points)}
          fill="transparent"
          stroke={r.id === active ? "var(--color-warning-on-dark)" : "rgb(255 255 255 / 0.85)"}
          strokeWidth={r.id === active ? 3 : 2}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          className={onSelect ? "cursor-pointer" : undefined}
          onClick={onSelect && (() => onSelect(r.id))}
        >
          <title>{r.label}</title>
        </polygon>
      ))}
    </svg>
  );
}

/** 問答的參考來源：畫作縮圖上框出這段講的是哪一塊 */
export function RegionThumb({
  artworkId,
  region,
  compact = false,
}: {
  artworkId: string;
  region: RegionShape;
  compact?: boolean;
}) {
  return (
    <div className="mt-1 flex items-end gap-2">
      <div className="relative w-fit shrink-0">
        <img
          src={assetUrl(`/api/v1/artworks/${encodeURIComponent(artworkId)}/image?size=thumb`)}
          alt={`畫作縮圖，框出「${region.label}」`}
          loading="lazy"
          className={`block w-auto rounded ${compact ? "h-16" : "h-24"}`}
        />
        <RegionOverlay regions={[region]} active={region.id} />
      </div>
      <span className="text-ink-48">
        這段講的是畫面{regionWhere(region.points)}的「{region.label}」
      </span>
    </div>
  );
}
