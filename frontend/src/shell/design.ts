/**
 * 整合版的畫面與文案（來源：ArtRAG-前端demo/source/src/design.ts 的「整合版：入口與兩個模組」）。
 * 入口用 01 Quiet、工廠模組用 10 Night、藝術模組在 10 Night 上換暖色；視覺都在 styles/*.css。
 */
export type Domain = "factory" | "art";
export type View = "entry" | Domain;

export const MODULE: Record<Domain, { label: string; spaced: string; en: string; asks: string[]; pitch: string }> = {
  factory: {
    label: "工廠模組",
    spaced: "工 廠 模 組",
    en: "FACTORY MODULE",
    asks: ["圖紙", "3D", "庫存", "排程"],
    pitch: "大圖檢視圖紙，繼續追問 3D、庫存與排程",
  },
  art: {
    label: "藝術模組",
    spaced: "藝 術 模 組",
    en: "ART MODULE",
    asks: ["作品", "細看", "年表", "相似作品"],
    pitch: "在展牆上細看作品，繼續追問技法、年表與相似作品",
  },
};

/**
 * 每個模組的完整功能頁（原本「深入」能到的頁面）：模組頁首的「功能頁」選單、左側對話紀錄的模組捷徑共用這一份。
 * 一樣經過後端的權限檢查；icon 是 components/shell/Icons 的名稱
 */
export const TOOLS: Record<Domain, { label: string; to: string; icon: string }[]> = {
  factory: [
    { label: "圖紙查找", to: "/drawings/search", icon: "search" },
    { label: "3D 重建", to: "/reconstruct", icon: "cube" },
    { label: "庫存・訂單・工單查詢", to: "/inventory", icon: "box" },
    { label: "生產排程", to: "/schedule", icon: "calendar" },
    { label: "主管核准", to: "/approvals", icon: "shield" },
    { label: "照片建檔（圖紙）", to: "/drawings/intake", icon: "camera" },
    { label: "批次辨識", to: "/batch", icon: "layers" },
    { label: "兩件並排比較", to: "/compare-items", icon: "grid" },
    { label: "系統狀態", to: "/admin", icon: "cpu" },
  ],
  art: [
    { label: "以文搜畫・以圖搜圖", to: "/search", icon: "search" },
    { label: "比對兩張照片", to: "/photo-diff", icon: "image" },
    { label: "照片建檔（畫作）", to: "/artworks/intake", icon: "camera" },
    { label: "批次辨識", to: "/batch", icon: "layers" },
    { label: "兩件並排比較", to: "/compare-items", icon: "grid" },
    { label: "策略比較", to: "/compare", icon: "table" },
    { label: "系統狀態", to: "/admin", icon: "cpu" },
  ],
};

export interface Copy {
  placeholder: string;
  placeholderPhoto: string;
  attachLabel: string;
  sendLabel: string;
  followups: string;
}

export const COPY: Record<View, Copy> = {
  entry: {
    placeholder: "問畫作、圖紙、庫存，或交辦一件事",
    placeholderPhoto: "想問這張照片什麼？（可以不填）",
    attachLabel: "附上照片",
    sendLabel: "送出",
    followups: "接著問",
  },
  factory: {
    placeholder: "繼續問這個零件，或要求看圖紙、3D、庫存、排程",
    placeholderPhoto: "想問這張圖紙什麼？（可以不填）",
    attachLabel: "附上圖紙",
    sendLabel: "提問",
    followups: "接著問",
  },
  art: {
    placeholder: "繼續問這幅畫，或要求細看、年表、相似作品",
    placeholderPhoto: "想問這幅畫什麼？（可以不填）",
    attachLabel: "附上照片",
    sendLabel: "提問",
    followups: "接著看",
  },
};

export function greeting(name: string) {
  const h = new Date().getHours();
  const hi = h < 11 ? "早安" : h < 18 ? "午安" : "晚安";
  return { title: `${hi}，${name}。`, lede: "畫作、圖紙、庫存與生產，一句話交給我。" };
}

/** 功能頁屬於哪個模組的外觀：畫作相關用藝術模組，其他（圖紙、庫存、排程、核准、系統）用工廠模組 */
export function domainOfPath(pathname: string): Domain {
  return /^\/(search|artworks|photo-diff|compare(\/|$|\?))/.test(pathname) ? "art" : "factory";
}

/** 網址對應的畫面：/ 與 /c/<id> 是入口，/factory/<id>、/art/<id> 是模組，其他是功能頁（套模組外觀） */
export function viewOfPath(pathname: string): View {
  if (pathname === "/" || pathname.startsWith("/c/")) return "entry";
  if (pathname.startsWith("/factory/")) return "factory";
  if (pathname.startsWith("/art/")) return "art";
  return domainOfPath(pathname);
}
