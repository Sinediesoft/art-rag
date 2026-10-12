import type { CadHoles } from "../api/sse";

/** 孔位校正（docs/adr/035）的顯示文字：校正了顯示孔數，沒校正的原因放在 title；舊紀錄沒有這欄回 null */
export function holeStatus(holes?: CadHoles | null): { label: string; title?: string } | null {
  if (!holes) return null;
  if (holes.status === "applied") return { label: `已校正 ${holes.applied ?? holes.detected} 孔` };
  return { label: "未校正", title: holes.reason ?? undefined };
}
