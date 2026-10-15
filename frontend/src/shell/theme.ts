import artCss from "../styles/art.css?inline";
import entryCss from "../styles/entry.css?inline";
import moduleCss from "../styles/module.css?inline";
import type { View } from "./design";

/**
 * 入口（01 Quiet）、模組（10 Night）、藝術模組暖色三份主題都覆寫 :root 的 token，不能同時生效：
 * 和 ArtRAG-前端demo/index.html 一樣依畫面只啟用該用的那幾份（media 切成 not all 就不套用）。
 * 內容包進 artrag 層（index.css 的層級順序），排在 core.css／app.css 之後所以會覆寫它們。
 */
const SHEETS = { entry: entryCss, module: moduleCss, art: artCss } as const;

function sheet(id: keyof typeof SHEETS) {
  const key = `artrag-theme-${id}`;
  let el = document.getElementById(key) as HTMLStyleElement | null;
  if (!el) {
    el = document.createElement("style");
    el.id = key;
    el.textContent = `@layer artrag {\n${SHEETS[id]}\n}`;
    document.head.appendChild(el);
  }
  return el;
}

export type EntryTheme = "dark" | "light";

export function applyStyles(view: View, theme: EntryTheme) {
  // 依序建立：module 在 entry 之後、art 在 module 之後（同一層裡後面的覆寫前面的）
  const on = { entry: view === "entry", module: view !== "entry", art: view === "art" };
  for (const id of ["entry", "module", "art"] as const) sheet(id).media = on[id] ? "all" : "not all";
  const root = document.documentElement;
  root.dataset.view = view;
  // 入口與兩個模組共用同一個深淺色設定（模組的明亮模式見 module.css、art.css 的 :root[data-theme="light"]）
  root.dataset.theme = theme;
  const color = {
    entry: theme === "light" ? "#faf9f7" : "#0f0f0e",
    factory: theme === "light" ? "#f3f4f6" : "#000000",
    art: theme === "light" ? "#f7f7f6" : "#0f0c09",
  }[view];
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", color);
}
