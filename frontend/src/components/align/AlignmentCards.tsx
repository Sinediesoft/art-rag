// 影像對位與比對（docs/adr/012）：以圖搜圖辨識成功後，標出照片拍到參考圖的哪一塊，再列出不一樣的地方
// （畫作比形狀與顏色，圖紙比三視圖的線條）；兩張照片互比也用同一套
import { useId } from "react";
import { assetUrl, type ApiError, type ImageAlignment } from "../../api/client";
import { useImageAlignment } from "../../api/hooks";
import { whereOnPainting } from "../../lib/format";
import { ErrorMessage, Loading } from "../common/Feedback";

const pct = (x: number) => `${Math.max(1, Math.round(x * 100))}%`;

/** 對不上是確定的答案（不硬畫），只留一行小字；其他錯誤照一般錯誤顯示 */
function AlignError({ error, what }: { error: unknown; what: string }) {
  const e = error as ApiError | null;
  if (e?.code === "ALIGN_FAILED")
    return <p className="text-xs text-ink-48">照片和{what}對不上（對應的特徵點不夠），無法標出位置。</p>;
  return <ErrorMessage title="影像比對失敗" message={e?.message ?? ""} requestId={e?.requestId} code={e?.code} />;
}

function Footer({ data }: { data: ImageAlignment }) {
  return (
    <footer className="flex flex-col gap-0.5 text-xs text-ink-48">
      {data.notes.map((n) => (
        <p key={n}>{n}</p>
      ))}
      <p className="font-mono text-[11px]">
        對應點 {data.inliers} · {data.latency_ms} ms
      </p>
    </footer>
  );
}

/** 拍到的範圍：polygon 畫成框線；masked 時範圍外調暗（伺服器的差異疊圖已經調暗過，就只畫框線） */
function CoverageOutline({ polygon, masked }: { polygon: number[][]; masked: boolean }) {
  const maskId = useId();
  const points = polygon.map(([x, y]) => `${x},${y}`).join(" ");
  return (
    <svg
      viewBox="0 0 1 1"
      preserveAspectRatio="none"
      className="pointer-events-none absolute inset-0 h-full w-full rounded-md"
      aria-hidden
    >
      {masked && (
        <>
          <defs>
            <mask id={maskId}>
              <rect width="1" height="1" fill="white" />
              <polygon points={points} fill="black" />
            </mask>
          </defs>
          <rect width="1" height="1" fill="rgb(29 29 31 / 0.55)" mask={`url(#${maskId})`} />
        </>
      )}
      <polygon
        points={points}
        fill="none"
        stroke="var(--color-warning-soft)"
        strokeWidth={3}
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/** 畫作：知識庫原圖上框出照片拍到的範圍，再標出和原圖不一樣的地方（形狀、顏色），編號列出 */
export function PhotoLocation({ imageId, artworkId }: { imageId: string; artworkId: string }) {
  const { data, isLoading, error } = useImageAlignment(imageId, `artwork:${artworkId}`);
  if (isLoading) return <Loading label="比對照片拍到原畫的哪一塊、有沒有不一樣…" />;
  if (error || !data) return <AlignError error={error} what="原圖" />;
  const { coverage, polygon, center } = data.location;
  const whole = coverage >= 0.9;
  const diff = data.diff;
  const regions = toneRegions(diff);
  const showDiff = diff && diff.status !== "global_change";
  const summary = diff && {
    same: "拍到的範圍裡，和知識庫原圖一致，沒有找到不一樣的地方。",
    changed: `找到 ${regions.length} 處和知識庫原圖不一樣的地方（差異候選，請對照原圖確認）。`,
    global_change: "和原圖的差異遍布整張：可能光線差太多、大片反光，或照片太模糊，沒有逐處標出。",
  }[diff.status];

  return (
    <section className="card flex flex-col gap-3 p-5">
      <header>
        <h2 className="text-lg font-semibold">{diff ? "和知識庫原圖比對" : "你拍到的位置"}</h2>
        <p className="text-sm text-ink-80">
          {whole ? "照片拍到整幅畫。" : `照片拍到原畫約 ${pct(coverage)} 的範圍，在畫面的${whereOnPainting(center)}。`}
        </p>
        {summary && <p className={`text-sm ${diff.status === "same" ? "text-success" : "text-ink-80"}`}>{summary}</p>}
      </header>
      <div className={showDiff && regions.length > 0 ? "grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]" : ""}>
        <div className="relative mx-auto w-fit max-w-full">
          <img
            src={assetUrl(showDiff ? data.overlay_url : data.reference_url)}
            alt={showDiff ? "知識庫原圖上標出和照片不一樣的地方" : "知識庫原圖，框出照片拍到的範圍"}
            className="block max-h-[60vh] w-auto max-w-full rounded-md"
          />
          {!whole && <CoverageOutline polygon={polygon} masked={!showDiff} />}
        </div>
        {showDiff && <ToneRegionList regions={regions} />}
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
  const regions = diff.regions.filter((r): r is typeof r & { kind: keyof typeof KIND } => r.kind in KIND);
  const n = regions.length;
  const summary = {
    same: "三視圖的線條一致，沒有找到差異。",
    changed: `找到 ${n} 處和知識庫圖紙不一樣的地方。`,
    global_change: "差異遍布整張圖，可能改了外形尺寸。",
  }[diff.status];

  return (
    <section className="card flex flex-col gap-3 p-5">
      <header>
        <h2 className="text-lg font-semibold">和知識庫圖紙（rev.{revision}）的差異</h2>
        <p className={`text-sm ${diff.status === "same" ? "text-success" : "text-ink-80"}`}>{summary}</p>
      </header>
      {diff.status !== "global_change" && (
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]">
          <img
            src={assetUrl(data.overlay_url)}
            alt="照片拉正後和知識庫圖紙的差異疊圖"
            className="mx-auto block max-h-[70vh] w-auto max-w-full rounded-md border border-hairline bg-white"
          />
          {n > 0 && (
            <ol className="flex flex-col gap-2 text-sm">
              {regions.map((r, i) => (
                <li key={r.bbox.join(",")} className="flex items-start gap-2">
                  <span
                    className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold text-white"
                    style={{ background: KIND[r.kind].color }}
                  >
                    {i + 1}
                  </span>
                  <span>
                    {KIND[r.kind].label}
                    <span className="ml-1 text-xs text-ink-48">· 占圖紙線條 {pct(r.area_ratio)}</span>
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

const TONE_KIND = {
  shape: { color: "#dc2626", label: "形狀不同（加筆、補筆、塗糊、多了東西）" },
  color: { color: "#d97706", label: "顏色不同（褪色、補色、反光）" },
  both: { color: "#9333ea", label: "形狀和顏色都不同" },
} as const;

type ToneRegion = NonNullable<ImageAlignment["diff"]>["regions"][number] & { kind: keyof typeof TONE_KIND };

/** 形狀、顏色的差異（tone）：其他 kind 濾掉 */
function toneRegions(diff: ImageAlignment["diff"]): ToneRegion[] {
  return (diff?.regions ?? []).filter((r): r is ToneRegion => r.kind in TONE_KIND);
}

/** 差異編號清單，編號和疊圖上的方框一致 */
function ToneRegionList({ regions }: { regions: ToneRegion[] }) {
  if (regions.length === 0) return null;
  return (
    <ol className="flex flex-col gap-2 text-sm">
      {regions.map((r, i) => (
        <li key={r.bbox.join(",")} className="flex items-start gap-2">
          <span
            className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold text-white"
            style={{ background: TONE_KIND[r.kind].color }}
          >
            {i + 1}
          </span>
          <span>
            {TONE_KIND[r.kind].label}
            <span className="ml-1 text-xs text-ink-48">· 占比對範圍 {pct(r.area_ratio)}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

/** 兩張照片互比（畫作）：照片 B 對齊到照片 A 之後比形狀與顏色，疊圖畫在照片 A 上 */
export function PairDiff({ a, b }: { a: string; b: string }) {
  const { data, isLoading, error } = useImageAlignment(b, `image:${a}`);
  if (isLoading) return <Loading label="對齊兩張照片、比對形狀與顏色…" />;
  if (error || !data) {
    const e = error as ApiError | null;
    if (e?.code === "ALIGN_FAILED")
      return (
        <ErrorMessage
          title="兩張照片對不上"
          message="對應的特徵點不夠：可能不是同一幅畫，或兩張拍到的地方重疊太少。"
          requestId={e.requestId}
        />
      );
    return <ErrorMessage title="照片比對失敗" message={e?.message ?? ""} requestId={e?.requestId} code={e?.code} />;
  }
  const diff = data.diff;
  if (!diff) return null;
  const regions = toneRegions(diff);
  const summary = {
    same: "兩張照片拍到的範圍裡，形狀和顏色都一致。",
    changed: `找到 ${regions.length} 處不一樣的地方。`,
    global_change: "差異遍布整張：可能光線差太多，或拍的不是同一個地方。",
  }[diff.status];

  return (
    <section className="card flex flex-col gap-3 p-5">
      <header>
        <h2 className="text-lg font-semibold">比對結果</h2>
        <p className={`text-sm ${diff.status === "same" ? "text-success" : "text-ink-80"}`}>{summary}</p>
        <p className="text-xs text-ink-48">照片 B 拍到照片 A 的 {pct(data.location.coverage)}，只比重疊的地方；結果畫在照片 A 上。</p>
      </header>
      {diff.status !== "global_change" && (
        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]">
          <img
            src={assetUrl(data.overlay_url)}
            alt="照片 A 上標出和照片 B 不一樣的地方"
            className="mx-auto block max-h-[70vh] w-auto max-w-full rounded-md"
          />
          <ToneRegionList regions={regions} />
        </div>
      )}
      <Footer data={data} />
    </section>
  );
}
