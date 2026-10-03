import { Link } from "react-router-dom";
import type { RouteInfo } from "../../api/client";
import type { DoneEvent } from "../../api/sse";

const DOMAIN_LABEL = { art: "畫作", mfg: "工廠圖紙" } as const;

/** 領域路由的提示：照片被自動轉到另一個領域、或判斷不確定時顯示，並提供改用另一邊辨識的連結 */
export function RouteNotice({
  route,
  imageId,
  redirected,
}: {
  route: RouteInfo;
  imageId: string;
  redirected: boolean;
}) {
  if (!redirected && !route.uncertain) return null;
  const other = route.domain === "art" ? "mfg" : "art";
  const label = DOMAIN_LABEL[route.domain];
  const title = route.uncertain
    ? "不太確定這是畫作還是工廠圖紙，先當成工廠圖紙處理"
    : `這張照片看起來是${label}，已自動改用${label}辨識`;
  const to =
    other === "art" ? `/search?image=${imageId}&domain=art` : `/drawings/search?image=${imageId}&domain=mfg`;
  const fmt = (x: number | null) => (x == null ? "—" : x.toFixed(2));
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-warning/30 bg-warning-soft px-4 py-3">
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        <p className="text-xs text-ink-80">
          與畫作的相似度 {fmt(route.art_score)} · 與圖紙的相似度 {fmt(route.mfg_score)}
          {route.uncertain && "；圖紙屬機密，不確定時走圖紙流程，不會送往任何雲端"}
        </p>
      </div>
      <Link to={to} className="link shrink-0 text-sm underline-offset-2">
        不對？改用{DOMAIN_LABEL[other]}辨識 →
      </Link>
    </div>
  );
}

/** 「知識庫中沒有這幅畫」固定樣式，所有頁面共用 */
export function NotInKbNotice({ detail, title = "知識庫中沒有這幅畫" }: { detail?: string; title?: string }) {
  return (
    <div className="flex gap-3 rounded-xl border border-hairline bg-parchment-deep/60 p-4">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-card font-display text-lg text-ink-80">
        ？
      </div>
      <div>
        <p className="text-lg font-semibold">{title}</p>
        <p className="text-sm text-ink-80">
          {detail ?? "系統不會硬湊答案。可以換個角度重拍、減少反光，或改用文字描述搜尋。"}
        </p>
      </div>
    </div>
  );
}

/** 「本地備援模型」固定樣式：生成端不是使用者選的那一個時顯示（備援只在本地之間，不改走雲端） */
export function FallbackBadge({ done }: { done: DoneEvent }) {
  if (!done.fallback) return null;
  const label = done.strategy_used === "hybrid_fallback" ? "本地備援模型" : `備援：${done.strategy_used}`;
  return (
    <span
      title={done.fallback_reason ?? ""}
      className="inline-flex items-center gap-1 rounded-full bg-warning-soft px-2 py-0.5 text-xs font-semibold text-warning"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-warning" />
      {label}
    </span>
  );
}

/** 「資料外送」標示：本地策略顯示 0，雲端對照組顯示送往第三方的內容 */
export function EgressBadge({ egress }: { egress: DoneEvent["egress"] }) {
  const out = egress.images > 0 || egress.chunks > 0 || egress.bytes > 0;
  const kb = (egress.bytes / 1024).toFixed(egress.bytes < 10240 ? 1 : 0);
  // 只有第 4 段送 Jev（代號化公開段落，只判斷、不生成）：生成仍全在本機
  if (out && egress.jev_bytes && egress.jev_bytes === egress.bytes)
    return (
      <span
        title="只送代號化的公開段落讓 Jev 判斷夾帶指令與關聯性；照片、機密段落與生成都在本機"
        className="inline-flex items-center gap-1 rounded-full bg-warning-soft px-2 py-0.5 text-xs font-semibold text-warning"
      >
        外送 {kb} KB → Jev（公開段落 {egress.chunks} 段，只判斷）
      </span>
    );
  return (
    <span
      title={out ? `共 ${kb} KB 送往第三方雲端` : "照片、問題與知識庫全程留在本機"}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${
        out ? "bg-danger-soft text-danger" : "bg-success-soft text-success"
      }`}
    >
      {out ? `外送：照片 ${egress.images} 張、段落 ${egress.chunks} 段 → 第三方` : "外送 0 · 全在本地"}
    </span>
  );
}

export function VerifiedBadge({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${
        ok ? "bg-success-soft text-success" : "bg-parchment-deep text-ink-80"
      }`}
    >
      {ok ? "✓" : "·"} {children}
    </span>
  );
}
