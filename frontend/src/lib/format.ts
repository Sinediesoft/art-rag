// 伺服器一律傳 UTC，畫面顯示轉台灣時間（共用層 §五）
export const formatTaipei = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });

export const seconds = (ms: number | null | undefined) =>
  ms == null ? "—" : `${(ms / 1000).toFixed(1)} 秒`;

export const twd = (v: number) => (v === 0 ? "NT$0" : `NT$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`);

/** 0–1 座標落在畫面九宮格的哪一格（照片拍到的範圍、圈出的區域）；後端 chunking.where_on_painting() 用同一套詞 */
export function whereOnPainting([x, y]: number[]): string {
  const col = x < 1 / 3 ? 0 : x > 2 / 3 ? 2 : 1;
  const row = y < 1 / 3 ? 0 : y > 2 / 3 ? 2 : 1;
  return [
    ["左上", "上方", "右上"],
    ["左側", "中央", "右側"],
    ["左下", "下方", "右下"],
  ][row][col];
}

export const STRATEGY_LABEL: Record<string, string> = {
  hybrid: "混合式",
  hybrid_norag: "混合式（關檢索）",
  hybrid_plain: "混合式（段落篩選關）",
  hybrid_rearrange: "混合式（MIRA 段落篩選）",
  hybrid_fallback: "本地備援模型",
  api_nokb: "雲端・無檢索（A1）",
  api_kb: "雲端＋檢索（A2）",
  lora: "本地 LoRA",
  ortho2cad: "Ortho2CAD",
  mock: "mock",
};
