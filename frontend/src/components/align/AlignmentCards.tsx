// 影像對位與比對（docs/adr/012）：以圖搜圖辨識成功後，畫作標出照片拍到原畫的哪一塊，圖紙列出和知識庫圖紙的差異
import { useId } from "react";
import { assetUrl, type ApiError, type ImageAlignment } from "../../api/client";
import { useImageAlignment } from "../../api/hooks";
import { ErrorMessage, Loading } from "../common/Feedback";

const pct = (x: number) => `${Math.max(1, Math.round(x * 100))}%`;

/** 拍到的範圍中心落在原圖的哪一區（九宮格） */
function whereOnPainting([x, y]: number[]): string {
  const col = x < 1 / 3 ? 0 : x > 2 / 3 ? 2 : 1;
  const row = y < 1 / 3 ? 0 : y > 2 / 3 ? 2 : 1;
  return [
    ["左上", "上方", "右上"],
    ["左側", "中央", "右側"],
    ["左下", "下方", "右下"],
  ][row][col];
}

/** 對不上是確定的答案（不硬畫），只留一行小字；其他錯誤照一般錯誤顯示 */
function AlignError({ error, what }: { error: unknown; what: string }) {
  const e = error as ApiError | null;
  if (e?.code === "ALIGN_FAILED")
    return <p className="text-xs text-ink-faint">照片和{what}對不上（對應的特徵點不夠），無法標出位置。</p>;
  return <ErrorMessage title="影像比對失敗" message={e?.message ?? ""} requestId={e?.requestId} code={e?.code} />;
}

function Footer({ data }: { data: ImageAlignment }) {
  return (
    <footer className="flex flex-col gap-0.5 text-xs text-ink-faint">
      {data.notes.map((n) => (
        <p key={n}>{n}</p>
      ))}
      <p className="font-mono text-[11px]">
        對應點 {data.inliers} · {data.latency_ms} ms
      </p>
    </footer>
  );
}

/** 畫作：知識庫原圖上框出照片拍到的範圍，範圍外調暗 */
export function PhotoLocation({ imageId, artworkId }: { imageId: string; artworkId: string }) {
  const maskId = useId();
  const { data, isLoading, error } = useImageAlignment(imageId, `artwork:${artworkId}`);
  if (isLoading) return <Loading label="比對照片拍到原畫的哪一塊…" />;
  if (error || !data) return <AlignError error={error} what="原圖" />;
  const { coverage, polygon, center } = data.location;
  const whole = coverage >= 0.9;
  const points = polygon.map(([x, y]) => `${x},${y}`).join(" ");

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-line bg-card p-4">
      <header>
        <h2 className="font-serif text-lg font-bold">你拍到的位置</h2>
        <p className="text-sm text-ink-soft">
          {whole ? "照片拍到整幅畫。" : `照片拍到原畫約 ${pct(coverage)} 的範圍，在畫面的${whereOnPainting(center)}。`}
        </p>
      </header>
      <div className="relative mx-auto w-fit max-w-full">
        <img
          src={assetUrl(data.reference_url)}
          alt="知識庫原圖，框出照片拍到的範圍"
          className="block max-h-[60vh] w-auto max-w-full rounded-md"
        />
        {!whole && (
          <svg
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            className="pointer-events-none absolute inset-0 h-full w-full rounded-md"
            aria-hidden
          >
            <defs>
              <mask id={maskId}>
                <rect width="1" height="1" fill="white" />
                <polygon points={points} fill="black" />
              </mask>
            </defs>
            <rect width="1" height="1" fill="rgb(31 27 22 / 0.55)" mask={`url(#${maskId})`} />
            <polygon
              points={points}
              fill="none"
              stroke="var(--color-amber-soft)"
              strokeWidth={3}
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        )}
      </div>
      <Footer data={data} />
    </section>
  );
}

const KIND = {
  missing: { color: "#dc2626", label: "照片少了這些線條（知識庫圖紙有）" },
  extra: { color: "#2563eb", label: "照片多了這些線條（知識庫圖紙沒有）" },
  both: { color: "#9333ea", label: "線條不一樣（有少也有多）" },
} as const;

/** 圖紙：照片拉正後和知識庫圖紙比三視圖的線條，疊圖標出差異並編號 */
export function DrawingDiff({ imageId, partId, revision }: { imageId: string; partId: string; revision: string }) {
  const { data, isLoading, error } = useImageAlignment(imageId, `part:${partId}`);
  if (isLoading) return <Loading label="拉正照片、比對三視圖的線條…" />;
  if (error || !data) return <AlignError error={error} what="知識庫圖紙" />;
  const diff = data.diff;
  if (!diff) return null;
  const n = diff.regions.length;
  const summary = {
    same: "三視圖的線條一致，沒有找到差異。",
    changed: `找到 ${n} 處和知識庫圖紙不一樣的地方。`,
    global_change: "差異遍布整張圖，可能改了外形尺寸。",
  }[diff.status];

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-line bg-card p-4">
      <header>
        <h2 className="text-lg font-bold">和知識庫圖紙（rev.{revision}）的差異</h2>
        <p className={`text-sm ${diff.status === "same" ? "text-jade" : "text-ink-soft"}`}>{summary}</p>
      </header>
      {diff.status !== "global_change" && (
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]">
          <img
            src={assetUrl(data.overlay_url)}
            alt="照片拉正後和知識庫圖紙的差異疊圖"
            className="mx-auto block max-h-[70vh] w-auto max-w-full rounded-md border border-line bg-white"
          />
          {n > 0 && (
            <ol className="flex flex-col gap-2 text-sm">
              {diff.regions.map((r, i) => (
                <li key={r.bbox.join(",")} className="flex items-start gap-2">
                  <span
                    className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white"
                    style={{ background: KIND[r.kind].color }}
                  >
                    {i + 1}
                  </span>
                  <span>
                    {KIND[r.kind].label}
                    <span className="ml-1 text-xs text-ink-faint">· 占圖紙線條 {pct(r.area_ratio)}</span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      <Footer data={data} />
    </section>
  );
}
