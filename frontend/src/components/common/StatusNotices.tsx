import type { DoneEvent } from "../../api/sse";

/** 「知識庫中沒有這幅畫」固定樣式，所有頁面共用 */
export function NotInKbNotice({ detail, title = "知識庫中沒有這幅畫" }: { detail?: string; title?: string }) {
  return (
    <div className="flex gap-3 rounded-xl border border-dashed border-ink-faint/50 bg-paper-deep/60 p-4">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-card font-serif text-lg text-ink-soft">
        ？
      </div>
      <div>
        <p className="font-serif text-lg font-bold">{title}</p>
        <p className="text-sm text-ink-soft">
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
      className="inline-flex items-center gap-1 rounded-full bg-amber-soft px-2 py-0.5 text-xs font-bold text-amber"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-amber" />
      {label}
    </span>
  );
}

/** 「資料外送」標示：本地策略顯示 0，雲端對照組顯示送往第三方的內容 */
export function EgressBadge({ egress }: { egress: DoneEvent["egress"] }) {
  const out = egress.images > 0 || egress.chunks > 0 || egress.bytes > 0;
  const kb = (egress.bytes / 1024).toFixed(egress.bytes < 10240 ? 1 : 0);
  return (
    <span
      title={out ? `共 ${kb} KB 送往第三方雲端` : "照片、問題與知識庫全程留在本機"}
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-bold ${
        out ? "bg-seal-soft text-seal" : "bg-jade-soft text-jade"
      }`}
    >
      {out ? `外送：照片 ${egress.images} 張、段落 ${egress.chunks} 段 → 第三方` : "外送 0 · 全在本地"}
    </span>
  );
}

export function VerifiedBadge({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-bold ${
        ok ? "bg-jade-soft text-jade" : "bg-paper-deep text-ink-soft"
      }`}
    >
      {ok ? "✓" : "·"} {children}
    </span>
  );
}
