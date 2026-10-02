// 伺服器一律傳 UTC，畫面顯示轉台灣時間（共用層 §五）
export const formatTaipei = (iso: string) =>
  new Date(iso).toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });

export const seconds = (ms: number | null | undefined) =>
  ms == null ? "—" : `${(ms / 1000).toFixed(1)} 秒`;

export const twd = (v: number) => (v === 0 ? "NT$0" : `NT$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`);

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
