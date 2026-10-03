import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { usePartTextSearch, useRoutedSearch } from "../../api/hooks";
import { DrawingDiff } from "../../components/align/AlignmentCards";
import { ApiErrorMessage, Loading } from "../../components/common/Feedback";
import { NotInKbNotice, RouteNotice, VerifiedBadge } from "../../components/common/StatusNotices";
import { PartCard } from "../../components/parts/PartCard";

export function DrawingSearchPage() {
  const [params] = useSearchParams();
  const imageId = params.get("image");
  return imageId ? (
    <ImageResults
      imageId={imageId}
      forced={params.get("domain") === "mfg"}
      redirected={params.get("routed") === "1"}
    />
  ) : (
    <TextResults q={params.get("q") ?? ""} />
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
  // 預設先經過領域路由；使用者在路由提示按「改用工廠圖紙辨識」（domain=mfg）時直接做圖紙辨識
  const { data, route, isLoading, error, latencyMs, redirectTo } = useRoutedSearch("mfg", imageId, forced);
  const best = data?.matched ? data.results[0] : null;
  const others = data ? data.results.filter((r) => r !== best) : [];

  // 判定為畫作：轉到畫作辨識頁
  if (redirectTo) return <Navigate to={redirectTo} replace />;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-3">
        <img
          src={api.uploadedImageUrl(imageId)}
          alt="你上傳的圖紙"
          className="h-20 w-20 rounded-lg border border-hairline bg-canvas object-cover"
        />
        <div>
          <p className="t-eyebrow">以圖搜圖紙</p>
          <h1 className="t-display">辨識結果</h1>
          {data && (
            <p className="text-xs text-ink-48">
              {route && "領域路由 → "}Chinese-CLIP 粗篩 → ORB 幾何驗證 → 線條重合度 · {latencyMs} ms
            </p>
          )}
        </div>
      </div>

      {route && <RouteNotice route={route} imageId={imageId} redirected={redirected} />}
      {isLoading && <Loading label="辨識中：判斷畫作或圖紙、比對特徵點並拉正圖紙…" />}
      {error && <ApiErrorMessage error={error} />}

      {best && (
        <section className="card grid gap-6 p-6 sm:grid-cols-[240px_1fr] sm:p-8">
          <PartCard part={best.part} to={`/drawings/${best.part.id}`} />
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <VerifiedBadge ok>辨識成功</VerifiedBadge>
              <VerifiedBadge ok>對應點 {best.inliers}</VerifiedBadge>
              <VerifiedBadge ok>線條重合 {Math.round((best.overlap ?? 0) * 100)}%</VerifiedBadge>
            </div>
            <h2 className="t-display">{best.part.name_zh}</h2>
            <p className="text-ink-80">
              料號 {best.part.part_no} · 圖號 {best.part.drawing_no} rev.{best.part.revision} · {best.part.material}
            </p>
            <div className="mt-auto flex flex-wrap gap-2">
              <Link
                to={`/drawings/${best.part.id}/reconstruct?image=${imageId}`}
                className="btn-primary"
              >
                Ortho2CAD 3D 重建
              </Link>
              <Link
                to={`/drawings/${best.part.id}/chat?image=${imageId}`}
                className="btn-ghost"
              >
                問問這張圖
              </Link>
              <Link
                to={`/drawings/${best.part.id}`}
                className="btn-ghost"
              >
                圖紙資料
              </Link>
            </div>
          </div>
        </section>
      )}

      {best && <DrawingDiff imageId={imageId} partId={best.part.id} revision={best.part.revision} />}

      {data && !data.matched && (
        <>
          <NotInKbNotice
            title="知識庫中沒有這張圖紙"
            detail={`最接近的圖紙沒有通過驗證（需要對應點 ≥ ${data.min_inliers} 且線條重合 ≥ ${Math.round(
              data.min_overlap * 100,
            )}%），所以不回答製程問題、也不硬湊答案。`}
          />
          <Link
            to={`/reconstruct?image=${imageId}`}
            className="card flex items-center justify-between gap-3 px-5 py-4 font-semibold text-ink transition hover:border-ink-48"
          >
            <span>
              沒收錄也能用 Ortho2CAD 重建 3D
              <span className="block text-xs font-normal text-ink-80">
                由本地 Qwen3-VL 讀取圖上的尺寸標註，Ortho2CAD 建模
              </span>
            </span>
            <span aria-hidden className="text-xl text-accent">→</span>
          </Link>
          <Link
            to={`/drawings/intake?image=${imageId}`}
            className="card flex items-center justify-between px-4 py-3 font-semibold text-accent transition hover:border-accent"
          >
            <span>
              把這張圖紙建進知識庫
              <span className="block text-xs font-normal text-ink-80">
                本地 Qwen3-VL 讀標題欄，你確認後由主管收錄；之後拍同一張就認得出來
              </span>
            </span>
            <span aria-hidden>→</span>
          </Link>
        </>
      )}

      {others.length > 0 && (
        <section>
          <h2 className="t-caption-strong mb-2 text-ink-80">
            {data?.matched ? "其他候選" : "最接近的圖紙（未通過驗證，僅供參考）"}
          </h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {others.map((r) => (
              <PartCard
                key={r.part.id}
                part={r.part}
                to={`/drawings/${r.part.id}`}
                dim
                footer={
                  <p className="text-xs text-ink-48">
                    相似度 {r.score.toFixed(2)} · 對應點 {r.inliers ?? "—"}
                    {r.overlap != null && ` · 重合 ${Math.round(r.overlap * 100)}%`}
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
  const { data, isLoading, error } = usePartTextSearch(q || null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim()) navigate(`/drawings/search?q=${encodeURIComponent(input.trim())}`);
  };
  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={submit} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="field min-w-0 flex-1"
          placeholder="用文字找圖紙：材料、工序、用途…"
        />
        <button className="btn-primary shrink-0">搜尋</button>
      </form>
      <div>
        <p className="t-eyebrow">以文字找圖紙</p>
        <h1 className="t-display">「{q}」</h1>
        {data && <p className="text-xs text-ink-48">bge-m3（文字→製程與檢驗段落） · {data.latency_ms} ms</p>}
      </div>
      {isLoading && <Loading label="搜尋中…" />}
      {error && <ApiErrorMessage error={error} />}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {data?.results.map((r, i) => (
          <PartCard
            key={r.part.id}
            part={r.part}
            to={`/drawings/${r.part.id}`}
            badge={
              i === 0 ? (
                <span className="rounded bg-accent px-1.5 py-0.5 text-[11px] font-semibold text-white">最相符</span>
              ) : undefined
            }
            footer={
              <p className="line-clamp-3 text-xs text-ink-80">
                <span className="font-semibold text-ink">{r.topic}</span> · {r.snippet}…
              </p>
            }
          />
        ))}
      </div>
    </div>
  );
}
