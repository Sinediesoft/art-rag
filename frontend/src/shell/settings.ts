import { useSyncExternalStore } from "react";
import type { ChatRequest } from "../api/client";

/**
 * 設定面板（頁首右上角，Q-2026-10-12-02）的問答開關：段落篩選、矛盾檢查、回答附圖。
 * - 存在這個瀏覽器（localStorage `artrag-settings-v1`），不跟帳號走：展示帳號是共用的，跟帳號走會讓別人也被改到；
 *   切換身分不清。只有開關的選擇，沒有任何資料。
 * - 「依伺服器預設」＝請求不帶這個欄位，由後端的 .env／models.yaml 決定（各主機不同）。
 * - 三個開關都不影響第 1～7 段安全關卡（docs/adr/030）。第 2、4～6 段由 Jev 還是地端規則判斷由伺服器決定，這裡沒有開關。
 * - 深淺色仍存在 `artrag-shell-v1`（shell/store.tsx），面板只是換個地方切換。
 */
export type Choice = "server" | "on" | "off";
export type ChatSwitch = "rearrange" | "conflict_check" | "send_image";
export type Settings = Record<ChatSwitch, Choice>;
/** 這一題實際套用的設定（沒列出的照伺服器預設或請求本身指定） */
export type Applied = Partial<Record<ChatSwitch, boolean>>;

export const SETTINGS_KEY = "artrag-settings-v1";
export const SWITCHES: ChatSwitch[] = ["rearrange", "conflict_check", "send_image"];
const DEFAULTS: Settings = { rearrange: "server", conflict_check: "server", send_image: "server" };

export const SWITCH_TEXT: Record<ChatSwitch, { label: string; desc: string; cost: string }> = {
  rearrange: {
    label: "段落篩選",
    desc: "回答前先請本地模型挑出有幫助的段落，只用挑中的（MIRA 的 Rearrange）",
    cost: "GTX 1650 每題約多 3–6 秒",
  },
  conflict_check: {
    label: "矛盾檢查",
    desc: "參考資料有 2 段以上時，先檢查說法有沒有互相矛盾，有的話回答會指出",
    cost: "5070 Ti 第一個字最多晚 0.6 秒",
  },
  send_image: {
    label: "回答附圖",
    desc: "已認出畫作或圖紙時，把圖一起送給生成模型；答案主要來自段落與作品卡",
    cost: "每題約多 1,070 token，4 GB 顯卡可以關",
  },
};

const isChoice = (v: unknown): v is Choice => v === "server" || v === "on" || v === "off";

function parse(raw: string | null): Settings {
  try {
    const o = JSON.parse(raw ?? "null") as Record<string, unknown> | null;
    if (!o || typeof o !== "object") return DEFAULTS;
    return { rearrange: pick(o.rearrange), conflict_check: pick(o.conflict_check), send_image: pick(o.send_image) };
  } catch {
    return DEFAULTS;
  }
}
const pick = (v: unknown): Choice => (isChoice(v) ? v : "server");

// 瀏覽器存不了（無痕視窗、封鎖網站資料）時記在這裡：這個分頁照樣生效，重新整理後回到預設
let memory: string | null = null;
let cache: { raw: string | null; value: Settings } | null = null;
const listeners = new Set<() => void>();

function rawValue(): string | null {
  try {
    return localStorage.getItem(SETTINGS_KEY) ?? memory;
  } catch {
    return memory;
  }
}

/** 目前的設定；內容沒變時回傳同一個物件（useSyncExternalStore 要求） */
export function getSettings(): Settings {
  const raw = rawValue();
  if (!cache || cache.raw !== raw) cache = { raw, value: parse(raw) };
  return cache.value;
}

export function setSetting(key: ChatSwitch, value: Choice) {
  const json = JSON.stringify({ ...getSettings(), [key]: value });
  try {
    localStorage.setItem(SETTINGS_KEY, json);
  } catch {
    memory = json;
  }
  listeners.forEach((l) => l());
}

const onStorage = (e: StorageEvent) => {
  // 其他分頁改了設定（或清除網站資料）
  if (e.key === SETTINGS_KEY || e.key === null) listeners.forEach((l) => l());
};

function subscribe(l: () => void) {
  if (listeners.size === 0) window.addEventListener("storage", onStorage);
  listeners.add(l);
  return () => {
    listeners.delete(l);
    if (listeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

export const useSettings = () => useSyncExternalStore(subscribe, getSettings, getSettings);

/**
 * 把設定套進問答請求：請求本身已指定的欄位（策略比較頁固定用伺服器預設）不動，
 * 設成「依伺服器預設」的不帶。回傳實際套用的，顯示在回答下方。
 */
export function withChatSettings(body: ChatRequest, s: Settings = getSettings()): { body: ChatRequest; applied: Applied } {
  const out: ChatRequest = { ...body };
  const applied: Applied = {};
  for (const k of SWITCHES) {
    if (body[k] !== undefined || s[k] === "server") continue;
    applied[k] = s[k] === "on";
    out[k] = applied[k];
  }
  return { body: out, applied };
}

/** 「依你的設定：段落篩選 關・回答附圖 開」；沒有套用任何設定時回傳 null */
export function appliedText(a: Applied | null | undefined): string | null {
  const items = SWITCHES.filter((k) => a?.[k] !== undefined).map((k) => `${SWITCH_TEXT[k].label} ${a![k] ? "開" : "關"}`);
  return items.length ? `依你的設定：${items.join("・")}` : null;
}
