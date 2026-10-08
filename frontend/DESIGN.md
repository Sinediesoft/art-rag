---
version: 2026-10-08
name: ArtRAG-integrated
source: ArtRAG-前端demo/（整合版：入口 01 Quiet、工廠模組 10 Night、藝術模組暖色）
description: 入口是安靜的單一墨色文件風格（預設深色、可切淺色），模組是夜間展牆：左邊純黑的對話欄，右邊暗色展示區，所有文字都很小、很安靜，成果才是主角。工廠模組的重點色是訊號紅，藝術模組換成暖深褐黑、象牙白與金箔色。

views:
  entry:
    sheet: src/styles/entry.css
    theme: dark（預設）／light（右上角切換，存在瀏覽器）
    bg: "#0f0f0e"
    text: "#f1efea"
    accent: "#f1efea"
    font: Inter + Noto Sans TC；mono IBM Plex Mono
  factory:
    sheet: src/styles/module.css
    bg: "#000000"
    show-bg: "#121212"
    text: "#ededed"
    mod: "#e5533d"（訊號紅）
    font: Noto Sans TC；mono IBM Plex Mono
  art:
    sheet: src/styles/module.css + src/styles/art.css
    bg: "#0f0c09"
    surface: "#17130f"
    text: "#ede6d8"（象牙白）
    mod: "#c9a45c"（金箔）
    display: Noto Serif TC（展品標籤、標題）
---

# ArtRAG 前端設計規則

介面來源是工作根目錄的 `ArtRAG-前端demo/`。版面、元件與 CSS 由它移植（`src/styles/core.css`、`app.css`、`entry.css`、
`module.css`、`art.css` 與 `src/components/shell/`）；資料一律來自後端 API／SSE，不用 demo 的 `mock/`、`assets/app.js`、
`assets/models.js`。決策見 `docs/adr/032-integrated-frontend.md`。

## 三種畫面

| 畫面 | 網址 | 版面 | 樣式表 |
|---|---|---|---|
| 入口 | `/`、`/c/<id>` | 左側對話紀錄（桌機可收合成窄欄、手機抽屜）＋問候＋輸入框＋建議問句；問了之後只回簡介與跳轉按鈕 | `entry.css` |
| 工廠模組 | `/factory/<id>` | 上方 `mtop`；左 `mchat`（對象、對話、輸入框）、中 `msplit`（30%–70%，雙擊 50%）、右 `showcase`（目前成果＋縮圖歷程） | `module.css` |
| 藝術模組 | `/art/<id>` | 同工廠模組，只換顏色與字 | `module.css`＋`art.css` |
| 功能頁 | `/search`、`/inventory`… | 模組的 `mtop`＋可捲動的 `feature`；內容是原本的功能頁（Tailwind） | 所屬模組（畫作相關＝藝術，其他＝工廠） |

窄於 900px：模組改成「對話／展示區」兩個分頁，展示區有新成果時分頁標「新」。窄於 1024px：入口的紀錄欄改成抽屜。

## Token

所有顏色、字體、圓角都是 CSS 變數，由當下的樣式表決定；元件只用變數，不寫死色碼（展品標籤的象牙白紙卡除外）。

| 變數 | 用途 |
|---|---|
| `--bg`、`--surface`、`--surface-2`、`--surface-3` | 底色與三層表面 |
| `--text`、`--text-2`、`--text-3` | 主文字、次要、提示 |
| `--line`、`--line-2` | 細線 |
| `--accent`、`--on-accent` | 互動色（整合版是淺色墨水，字用 `--on-accent`） |
| `--mod`、`--on-mod` | 模組識別色（工廠訊號紅、藝術金箔）：圓點、選中的縮圖、標註編號、跳轉按鈕 |
| `--factory`、`--art` | 入口上兩顆跳轉按鈕各自的顏色，還沒按就知道會去哪一邊 |
| `--ok`、`--warn`、`--danger` | 只用在狀態：通過、提醒、擋下 |
| `--series-1`…`--series-5` | 圖表 |

功能頁的 Tailwind 色票（`bg-card`、`text-ink-80`、`border-hairline`、`bg-accent`…）在 `src/index.css` 的 `@theme`
接到上面的變數，所以功能頁跟著模組變色。`text-white` 對到 `--on-accent`（整合版的重點色是淺色）。

## 樣式分層

`src/index.css` 宣告 `@layer theme, base, artrag, components, utilities`：

- `artrag`：整合版的 core／app／shell 與目前畫面的主題（`src/shell/theme.ts` 依畫面切 `media`，只啟用該用的那幾份）
- `utilities`：Tailwind，排在 artrag 之後，功能頁的 utility 不會被整合版的元素重設蓋掉
- 新增的元件樣式放 `src/styles/shell.css`（接真實 API 才有的狀態：等待中、讀不到、降級、功能頁連結、身分選單…）

## 元件

| 元件 | 檔案 | 規則 |
|---|---|---|
| 對話紀錄 | `components/shell/Sidebar.tsx` | 新對話、搜尋、依今天／昨天／這週分組、開啟、刪除（確認後清掉記憶體與瀏覽器紀錄）；窄欄只留圖示與最近 8 段的首字 |
| 輸入框 | `components/shell/Composer.tsx` | Enter 送出、Shift+Enter 換行、IME 組字中不送；照片先上傳、預覽用伺服器網址；回答中送出鈕變停止 |
| 處理軌跡 | `components/shell/Trace.tsx` | 一行七段（認證授權 › Jev Choice › Metadata Filter › Jev Noul › Jev Score › 生成閘門 › 本地 LLM）＋摘要＋外送量；展開看每段檢查，技術細節再收一層 |
| 跳轉按鈕 | `components/shell/Jump.tsx` | 只在七段流程分派到工廠或藝術、且沒有被擋下或降級時出現 |
| 展示區 | `components/shell/Showcase.tsx`、`Viewers.tsx` | 目前成果放大、下載、全螢幕；縮圖歷程可點、← → 切換；新成果完成自動成為目前成果 |
| 大圖 | `components/shell/ZoomPan.tsx` | 滾輪以游標為中心縮放、拖曳平移、雙擊放大／還原、按鈕縮放 |

## 互動與無障礙

- 所有按鈕都有可讀的名稱（`aria-label`）；分隔線是 `role="separator"`，可用 ← → 調寬、Enter／Home 回到一半。
- 焦點樣式：`:focus-visible` 2px `--accent` 外框，不要拿掉。
- `prefers-reduced-motion: reduce`：關掉淡入、轉場（進模組直接切換）、串流游標閃爍。
- 色彩不是唯一的狀態訊號：軌跡的擋下有 ✕ 與「擋下」文字、低於門檻有文字說明。

## 不要做

- 不要在前端產生通過、拒絕或結果：沒有回應的段落是「等待中」或「沒有執行」。
- 不要加 Jev／地端守門的手動切換：第 2、4～6 段由誰判斷由伺服器決定。
- 不要把 JWT、`route_ticket`、Jev 請求本文、內部資料或 `blob:` 網址存進 `localStorage`（規則在 `src/shell/persist.ts`）。
- 不要讓看不到的圖紙出現在按鈕、縮圖或標題上：圖紙只取第 1 段確認看得到的那一張（`auth.filter.doc_level`）。
