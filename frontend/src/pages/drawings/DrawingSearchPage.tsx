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
          className="h-20 w-20 rounded-lg border border-line bg-white object-cover shadow-sm"
        />
        <div>
          <p className="text-xs text-ink-faint">以圖搜圖紙</p>
          <h1 className="text-2xl font-bold">辨識結果</h1>
          {data && (
            <p className="text-xs text-ink-faint">
              {route && "領域路由 → "}Chinese-CLIP 粗篩 → ORB 幾何驗證 → 線條重合度 · {latencyMs} ms
            </p>
          )}
        </div>
      </div>

      {route && <RouteNotice route={route} imageId={imageId} redirected={redirected} />}
      {isLoading && <Loading label="辨識中：判斷畫作或圖紙、比對特徵點並拉正圖紙…" />}
      {error && <ApiErrorMessage error={error} />}

      {best && (
        <section className="grid gap-4 rounded-2xl border border-steel/30 bg-steel-soft/50 p-4 sm:grid-cols-[240px_1fr]">
          <PartCard part={best.part} to={`/drawings/${best.part.id}`} />
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <VerifiedBadge ok>辨識成功</VerifiedBadge>
              <VerifiedBadge ok>對應點 {best.inliers}</VerifiedBadge>
              <VerifiedBadge ok>線條重合 {Math.round((best.overlap ?? 0) * 100)}%</VerifiedBadge>
            </div>
            <h2 className="text-2xl font-black">{best.part.name_zh}</h2>
            <p className="text-ink-soft">
              料號 {best.part.part_no} · 圖號 {best.part.drawing_no} rev.{best.part.revision} · {best.part.material}
            </p>
            <div className="mt-auto flex flex-wrap gap-2">
              <Link
                to={`/drawings/${best.part.id}/reconstruct?image=${imageId}`}
                className="rounded-xl bg-steel px-4 py-2.5 font-bold text-white transition hover:bg-steel-deep"
              >
                Ortho2CAD 3D 重建
              </Link>
              <Link
                to={`/drawings/${best.part.id}/chat?image=${imageId}`}
                className="rounded-xl bg-ink px-4 py-2.5 font-bold text-paper transition hover:bg-ink/85"
              >
                問問這張圖
              </Link>
              <Link
                to={`/drawings/${best.part.id}`}
                className="rounded-xl border border-ink/15 bg-card px-4 py-2.5 font-bold transition hover:border-ink/40"
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
            className="flex items-center justify-between rounded-xl border border-steel/30 bg-steel-soft/60 px-4 py-3 font-bold text-steel-deep transition hover:border-steel"
          >
            <span>
              沒收錄也能用 Ortho2CAD 重建 3D
              <span className="block text-xs font-normal text-ink-soft">
                由本地 Qwen3-VL 讀取圖上的尺寸標註，Ortho2CAD 建模
              </span>
            </span>
            <span aria-hidden>→</span>
          </Link>
        </>
      )}

      {others.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-bold text-ink-soft">
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
                  <p className="text-xs text-ink-faint">
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
          className="min-w-0 flex-1 rounded-xl border border-line bg-card px-4 py-3 outline-none focus:border-steel"
          placeholder="用文字找圖紙：材料、工序、用途…"
        />
        <button className="shrink-0 rounded-xl bg-ink px-4 py-3 font-bold text-paper">搜尋</button>
      </form>
      <div>
        <p className="text-xs text-ink-faint">以文字找圖紙</p>
        <h1 className="text-2xl font-bold">「{q}」</h1>
        {data && <p className="text-xs text-ink-faint">bge-m3（文字→製程與檢驗段落） · {data.latency_ms} ms</p>}
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
                <span className="rounded bg-steel px-1.5 py-0.5 text-[11px] font-bold text-white">最相符</span>
              ) : undefined
            }
            footer={
              <p className="line-clamp-3 text-xs text-ink-soft">
                <span className="font-bold text-steel">{r.topic}</span> · {r.snippet}…
              </p>
            }
          />
        ))}
      </div>
    </div>
  );
}
