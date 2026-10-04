import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, assetUrl } from "../api/client";
import { useRoutedSearch, useTextSearch } from "../api/hooks";
import { PhotoLocation } from "../components/align/AlignmentCards";
import { PhotoColors } from "../components/color/ColorAnalysisCard";
import { ArtworkCard } from "../components/common/ArtworkCard";
import { ErrorMessage, Loading } from "../components/common/Feedback";
import { NotInKbNotice, RouteNotice, VerifiedBadge } from "../components/common/StatusNotices";
import { PhotoStyleGuess } from "../components/style/StyleGuessCard";

export function SearchPage() {
  const [params] = useSearchParams();
  const imageId = params.get("image");
  const q = params.get("q");
  return imageId ? (
    <ImageResults
      imageId={imageId}
      forced={params.get("domain") === "art"}
      redirected={params.get("routed") === "1"}
    />
  ) : (
    <TextResults q={q ?? ""} />
  );
}

function ImageResults({
  imageId,
  forced,
  redirected,
}: {
  imageId: string;
  forced: boolean;
  redirected: boolean;
}) {
  // 預設先經過領域路由；使用者在路由提示按「改用畫作辨識」（domain=art）時直接做畫作辨識
  const { data, route, isLoading, error, latencyMs, redirectTo } = useRoutedSearch("art", imageId, forced);
  const best = data?.matched ? data.results[0] : null;
  const others = data ? data.results.filter((r) => r !== best) : [];

  // 判定為工廠圖紙：轉到圖紙辨識頁
  if (redirectTo) return <Navigate to={redirectTo} replace />;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-3">
        <img
          src={api.uploadedImageUrl(imageId)}
          alt="你上傳的照片"
          className="h-20 w-20 rounded-lg border border-hairline object-cover"
        />
        <div>
          <p className="t-eyebrow">以圖搜圖</p>
          <h1 className="t-display">辨識結果</h1>
          {data && (
            <p className="text-xs text-ink-48">
              {route && "領域路由 → "}Chinese-CLIP 粗篩 → ORB 幾何驗證 · {latencyMs} ms
            </p>
          )}
        </div>
      </div>

      {route && <RouteNotice route={route} imageId={imageId} redirected={redirected} />}
      {isLoading && <Loading label="辨識中：判斷畫作或圖紙、計算影像向量並比對特徵點…" />}
      {error && (
        <ErrorMessage message={(error as Error).message} requestId={(error as ApiError).requestId} />
      )}

      {best && (
        <section className="card grid gap-6 p-6 sm:grid-cols-[240px_1fr] sm:p-8">
          <Link
            to={`/artworks/${best.artwork.id}`}
            className="block self-start overflow-hidden rounded-lg bg-parchment-deep product-shadow"
          >
            <img
              src={assetUrl(best.artwork.thumb_url)}
              alt={best.artwork.title_zh}
              className="aspect-[4/3] w-full object-cover object-[center_20%]"
            />
          </Link>
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <VerifiedBadge ok>辨識成功</VerifiedBadge>
              <VerifiedBadge ok>相似度 {best.score.toFixed(3)}</VerifiedBadge>
              <VerifiedBadge ok>幾何驗證 {best.inliers} 個對應點</VerifiedBadge>
            </div>
            <h2 className="t-display">〈{best.artwork.title_zh}〉</h2>
            <p className="text-ink-80">
              {best.artwork.artist_zh}（{best.artwork.artist_en}）· {best.artwork.date_text}
            </p>
            <p className="-mt-2 text-sm text-ink-48">{best.artwork.collection}</p>
            <div className="mt-auto flex flex-wrap gap-2">
              <Link
                to={`/artworks/${best.artwork.id}/chat?image=${imageId}`}
                className="btn-primary"
              >
                問問這幅畫
              </Link>
              <Link
                to={`/artworks/${best.artwork.id}`}
                className="btn-ghost"
              >
                畫作介紹
              </Link>
            </div>
          </div>
        </section>
      )}

      {data && !data.matched && (
        <>
          <NotInKbNotice
            detail={`最接近的畫作沒有通過驗證（需要相似度 ≥ ${data.threshold} 且對應點 ≥ ${data.min_inliers}），所以不硬湊答案。`}
          />
          {/* 不硬湊答案，但可以給「看起來像什麼」：風格大類、題材、媒材的推測，標明沒有出處（ADR 018） */}
          <PhotoStyleGuess imageId={imageId} />
          <Link
            to={`/artworks/intake?image=${imageId}`}
            className="card flex items-center justify-between px-4 py-3 font-semibold text-accent transition hover:border-accent"
          >
            <span>
              把這幅畫建進知識庫
              <span className="block text-xs font-normal text-ink-80">
                在表單填畫名、作者、典藏與授權，主管收錄後之後拍同一幅就認得出來
              </span>
            </span>
            <span aria-hidden>→</span>
          </Link>
        </>
      )}

      {best && <PhotoLocation imageId={imageId} artworkId={best.artwork.id} />}

      {data && <PhotoColors imageId={imageId} matched={data.matched} />}

      {others.length > 0 && (
        <section>
          <h2 className="t-caption-strong mb-2 text-ink-80">
            {data?.matched ? "其他候選" : "最接近的畫作（未通過驗證，僅供參考）"}
          </h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {others.map((r) => (
              <ArtworkCard
                key={r.artwork.id}
                artwork={r.artwork}
                to={`/artworks/${r.artwork.id}`}
                dim
                footer={
                  <p className="text-xs text-ink-48">
                    相似度 {r.score.toFixed(3)} · 對應點 {r.inliers ?? "—"}
                  </p>
                }
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function TextResults({ q }: { q: string }) {
  const navigate = useNavigate();
  const [input, setInput] = useState(q);
  const { data, isLoading, error } = useTextSearch(q || null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim()) navigate(`/search?q=${encodeURIComponent(input.trim())}`);
  };
  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={submit} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="field min-w-0 flex-1"
          placeholder="用文字描述畫面"
        />
        <button className="btn-primary shrink-0">搜尋</button>
      </form>
      <div>
        <p className="t-eyebrow">以文搜圖</p>
        <h1 className="t-display">「{q}」</h1>
        {data && (
          <p className="text-xs text-ink-48">
            Chinese-CLIP（文字→畫面）＋ bge-m3（文字→知識段落）以 RRF 融合排序 · {data.latency_ms} ms
          </p>
        )}
      </div>
      {isLoading && <Loading label="搜尋中…" />}
      {error && (
        <ErrorMessage message={(error as Error).message} requestId={(error as ApiError).requestId} />
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {data?.results.map((r, i) => (
          <ArtworkCard
            key={r.artwork.id}
            artwork={r.artwork}
            to={`/artworks/${r.artwork.id}`}
            badge={
              i === 0 ? (
                <span className="rounded-full bg-accent px-2.5 py-0.5 text-xs font-semibold text-white">最相符</span>
              ) : undefined
            }
            footer={
              <div className="flex flex-col gap-1">
                <ScoreBar label="畫面" value={r.image_score} max={0.5} />
                <ScoreBar label="文字" value={r.text_score} max={1} />
              </div>
            }
          />
        ))}
      </div>
    </div>
  );
}

function ScoreBar({ label, value, max }: { label: string; value: number; max: number }) {
  const pct = Math.max(4, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2 text-[11px] text-ink-48">
      <span className="w-6 shrink-0">{label}</span>
      <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-parchment-deep">
        <span className="block h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </span>
      <span className="w-9 shrink-0 text-right tabular-nums">{value.toFixed(2)}</span>
    </div>
  );
}
