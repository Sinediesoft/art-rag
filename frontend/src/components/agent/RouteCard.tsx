import type { RouteResponse } from "../../api/client";

export const GATE_LABEL: Record<RouteResponse["gate"], { text: string; style: string }> = {
  direct: { text: "直接執行", style: "bg-jade-soft text-jade" },
  confirm: { text: "先出確認卡", style: "bg-steel-soft text-steel-deep" },
  modify: { text: "修改資料流程", style: "bg-seal-soft text-seal-deep" },
  clarify: { text: "請你選一下", style: "bg-amber-soft text-amber" },
  out_of_scope: { text: "超出範圍", style: "bg-paper-deep text-ink-soft" },
};

const bytes = (n: number) => (n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`);

/** System 1 的判斷：誰判斷的、意圖與信心、信心閘門的分流、送出本機的資料量 */
export function RouteCard({ route }: { route: RouteResponse }) {
  const gate = GATE_LABEL[route.gate];
  const cloud = route.engine === "jev";
  const pct = Math.round(route.confidence * 100);
  const codes = Object.entries(route.mapping);
  return (
    <div className="rounded-xl border border-line bg-paper/60 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-bold tracking-wide text-ink-faint">System 1</span>
        <span
          className={`rounded-full px-2 py-0.5 font-bold ${cloud ? "bg-[#fde8df] text-[#b5481f]" : "bg-steel-soft text-steel-deep"}`}
        >
          {route.engine_label}
        </span>
        <span className="text-ink-faint">
          {route.model} · {route.latency_ms.system1 ?? 0} ms
        </span>
        <span
          className={`ml-auto rounded-full px-2 py-0.5 font-mono font-bold ${
            route.egress.bytes ? "bg-[#fde8df] text-[#b5481f]" : "bg-jade-soft text-jade"
          }`}
          title={cloud ? "只送代號化文字與題目說明；照片、原始名稱、資料庫內容都不送" : "全程在本機"}
        >
          {route.egress.bytes ? `外送 ${bytes(route.egress.bytes)} → Jev` : "外送 0 B"}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold text-ink">{route.intent_label}</span>
        <div className="flex items-center gap-1.5" title={`門檻 ${route.threshold}`}>
          <div className="relative h-1.5 w-28 overflow-hidden rounded-full bg-line">
            <div
              className={`h-full rounded-full ${route.confidence >= route.threshold ? "bg-jade" : "bg-amber"}`}
              style={{ width: `${pct}%` }}
            />
            <div className="absolute top-0 h-full w-0.5 bg-ink/60" style={{ left: `${route.threshold * 100}%` }} />
          </div>
          <span className="font-mono text-ink-soft">
            {route.confidence.toFixed(2)}
            <span className="text-ink-faint"> / 門檻 {route.threshold.toFixed(2)}</span>
          </span>
        </div>
        <span className={`rounded-full px-2 py-0.5 font-bold ${gate.style}`}>→ {gate.text}</span>
        {route.modify_op && (
          <span className="rounded-full bg-seal-soft px-2 py-0.5 text-seal-deep">
            {String(route.dispatch.op_label ?? route.modify_op)}
          </span>
        )}
        {route.flags.overrides_rules && (
          <span className="rounded-full bg-seal px-2 py-0.5 font-bold text-white">偵測到想略過規則，已記錄</span>
        )}
      </div>
      <p className="mt-1 text-ink-faint">{route.gate_reason}</p>
      {route.fallback_reason && (
        <p className="mt-1 text-amber">
          {route.engine === "local" ? "改走本地路由：" : ""}
          {route.fallback_reason}
        </p>
      )}

      <details className="mt-2">
        <summary className="cursor-pointer text-ink-faint hover:text-ink">
          判斷細節：前幾名機率、{cloud ? "送 Jev 的內容" : "代號化文字"}
        </summary>
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex flex-wrap gap-1.5">
            {route.ranked.map((r) => (
              <span key={r.intent} className="rounded bg-card px-1.5 py-0.5 font-mono text-ink-soft ring-1 ring-line">
                {r.label} {r.prob.toFixed(2)}
              </span>
            ))}
          </div>
          <div>
            <p className="text-ink-faint">{cloud ? "送出的文字（代號化）" : "代號化後的文字（本地路由不外送）"}</p>
            <p className="mt-0.5 rounded bg-code px-2 py-1 font-mono text-[12px] text-[#d7e3ee]">{route.masked_text || "（只有照片）"}</p>
          </div>
          {codes.length > 0 && (
            <p className="text-ink-faint">
              代號對照（只留在本機）：
              {codes.map(([code, e]) => (
                <span key={code} className="ml-1.5 whitespace-nowrap">
                  <b className="font-mono text-ink-soft">{code}</b>＝{String((e as { label?: string }).label)}
                </span>
              ))}
            </p>
          )}
          {route.photo && (
            <p className="text-ink-faint">
              照片在本機辨識（不送 Jev）：
              <b className="text-ink-soft">
                {route.photo.kind === "art" ? "畫作" : route.photo.kind === "drawing" ? "工廠圖紙" : "無法辨識"}
                {route.photo.label ? `〈${String(route.photo.label)}〉` : ""}
              </b>
            </p>
          )}
          {route.jev_request && (
            <pre className="max-h-48 overflow-auto rounded bg-code p-2 font-mono text-[11px] text-[#d7e3ee]">
              {JSON.stringify(route.jev_request, null, 2)}
            </pre>
          )}
        </div>
      </details>
    </div>
  );
}
