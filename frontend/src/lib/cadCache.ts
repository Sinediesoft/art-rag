import type { CadCacheInfo, CadCandidate } from "../api/sse";
import { formatTaipei } from "./format";

/** 3D 重建結果快取（docs/adr/042）的顯示文字 */
export function candidateLabel(c: CadCandidate): string {
  return c.kind === "greedy" ? "greedy" : `重抽 temperature ${c.temperature}・seed ${c.seed}`;
}

/** 命中：沿用 <時間> 的結果・IoU x・候選來源；沒命中回 null */
export function cacheHitLabel(c?: CadCacheInfo | null): string | null {
  if (!c || c.status !== "hit") return null;
  const when = c.created_at ? formatTaipei(c.created_at) : "先前";
  const iou = c.iou != null ? `IoU ${c.iou.toFixed(2)}` : "主管採用";
  return `沿用 ${when} 的結果・${iou}・${candidateLabel(c.candidate)}`;
}

/** 沒命中時的說明（存成標準結果、沒存的原因、先前被否決） */
export function cacheNote(c?: CadCacheInfo | null): string | null {
  if (!c || c.status === "hit") return null;
  if (c.status === "stored" && c.source === "adopted") return "主管已採用，存成這張圖紙的標準結果";
  if (c.status === "stored") return `通過品質閘門，已存成這張圖紙的標準結果（${candidateLabel(c.candidate)}）`;
  if (c.status === "rejected_before") return c.reason ?? "這份程式碼先前被標記為不對，沒有寫入";
  return c.reason ? `沒有存成標準結果：${c.reason}` : null;
}
