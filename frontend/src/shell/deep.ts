import type { RouteResponse } from "../api/client";
import { dispatchOf } from "./runner";
import type { Turn } from "./types";

/**
 * 回答下方的「在功能頁打開」連結（原 ADR 016 的「深入」按鈕，邏輯不變）：七段流程交給哪個功能，就提供那個功能的完整頁面。
 * 第 1、2 段擋下、降級「查無資料」、閒聊短路、要澄清、超出範圍都不給。
 * 指定圖紙的連結只在第 1 段確認這張圖紙在憑證權限內時才給（auth.filter.doc_level 只有看得到才回傳）。
 */
export interface DeepAction {
  label: string;
  to: string;
  primary?: boolean;
}

/**
 * 這一輪要給的功能頁連結：出錯、第 6 段生成閘門降級「查無資料」、問答失敗都不給（ADR 016：降級不給按鈕），
 * 其他依 deepActions 的規則
 */
export function deepActionsFor(t: Turn): DeepAction[] {
  const r = t.route;
  if (!r || t.phase === "error" || t.phase === "routing" || t.phase === "running") return [];
  const p = t.part;
  if (p?.kind === "chat" && (p.done?.degraded || p.sources?.post_filter?.gate?.passed === false || p.error)) return [];
  return deepActions(r, t.imageId);
}

export function deepActions(route: RouteResponse, imageId: string | null): DeepAction[] {
  if (route.blocked || route.short_circuit || route.outcome !== "pass") return [];
  const photo: DeepAction[] = imageId && route.photo ? [{ label: "看照片辨識細節", to: `/search?image=${imageId}` }] : [];
  if (route.photo?.kind === "unknown" && !route.question)
    return [...photo.map((a) => ({ ...a, label: "看照片辨識細節（沒收錄也能推測風格、分析色彩、重建 3D）", primary: true })), ...intakeActions(route, imageId)];
  if (route.gate === "clarify" || route.gate === "out_of_scope") return [];
  const pair: DeepAction[] = imageId && route.photo?.kind === "art" ? [{ label: "和另一張照片比對", to: `/photo-diff?a=${imageId}` }] : [];
  const base = moduleActions(route, imageId);
  return [...(base.length ? [...base, ...photo] : photo), ...pair];
}

/** 照片辨識不到：把它建進知識庫（ADR 013）。工廠圖紙只給能用工廠領域的身分 */
function intakeActions(route: RouteResponse, imageId: string | null): DeepAction[] {
  if (!imageId) return [];
  const actions: DeepAction[] = [{ label: "拍照建檔：這是一幅畫", to: `/artworks/intake?image=${imageId}` }];
  if (route.account.domains.includes("mfg")) actions.push({ label: "拍照建檔：這是一張圖紙", to: `/drawings/intake?image=${imageId}` });
  return actions;
}

function moduleActions(route: RouteResponse, imageId: string | null): DeepAction[] {
  const d = dispatchOf(route);
  const img = imageId ? `?image=${imageId}` : "";
  const q = encodeURIComponent(d.question || route.question);
  const artSearch: DeepAction[] = q ? [{ label: "看完整搜尋結果", to: `/search?q=${q}`, primary: true }] : [];
  const partSearch: DeepAction[] = q ? [{ label: "看完整查找結果", to: `/drawings/search?q=${q}`, primary: true }] : [];
  const filter = route.auth.filter;
  const partVisible = !!d.part_id && filter?.doc_id === d.part_id && !!filter.doc_level;
  const part: DeepAction | null = partVisible ? { label: `打開〈${d.part_label ?? d.part_id}〉圖紙`, to: `/drawings/${d.part_id}`, primary: true } : null;

  switch (route.intent) {
    case "art_search":
      return artSearch;
    case "art_qa":
      return d.artwork_id
        ? [
            { label: `打開〈${d.artwork_label ?? d.artwork_id}〉`, to: `/artworks/${d.artwork_id}`, primary: true },
            { label: "繼續問這幅畫", to: `/artworks/${d.artwork_id}/chat${img}` },
          ]
        : artSearch;
    case "drawing_search":
      return part ? [part, ...partSearch.map((a) => ({ ...a, primary: false }))] : partSearch;
    case "drawing_qa":
      if (part) return [part, { label: "繼續問這張圖", to: `/drawings/${d.part_id}/chat${img}` }];
      return d.part_id ? [] : partSearch;
    case "data_query":
      return [{ label: "打開庫存・訂單・工單查詢", to: "/inventory", primary: true }];
    case "reconstruct":
      return [
        { label: d.part_id || imageId ? "在 3D 重建頁打開" : "打開 3D 重建（先上傳圖紙照片）", to: `${d.path ?? "/reconstruct"}${img}`, primary: true },
        ...(part ? [{ ...part, label: "先看圖紙與之前的重建結果", primary: false }] : []),
      ];
    case "schedule":
      return [{ label: "打開生產排程", to: "/schedule", primary: true }];
    case "modify": {
      const op = d.op ?? "";
      if (op.startsWith("stock_") || op === "so_update") return [{ label: "打開庫存・訂單・工單查詢", to: "/inventory" }];
      if (op.startsWith("wo_")) return [{ label: "打開生產排程", to: "/schedule" }];
      return [];
    }
    case "system":
      return [{ label: "打開系統狀態", to: "/admin#memory", primary: true }];
    case "batch_identify":
      return [{ label: "打開批次辨識", to: "/batch", primary: true }];
    case "compare":
      return [{ label: d.compare?.refs.length === 2 ? "看完整比較表、差異摘要與匯出" : "打開兩件並排比較", to: d.path ?? "/compare-items", primary: true }];
    default:
      return [];
  }
}
