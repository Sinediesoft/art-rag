// 匯出（docs/adr/017）：批次辨識 CSV、兩件並排比較、問答報告都在瀏覽器裡產生，不另外經過伺服器；
// 匯出了哪些資料由呼叫端另外記一筆稽核（api.logExport）。報告是單一 HTML 檔（圖片內嵌），
// 可以直接存檔，也可以用瀏覽器列印存成 PDF。

/** HTML 跳脫：報告裡的文字都來自知識庫與模型輸出，不能當成標記 */
export function esc(s: unknown): string {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** CSV 一格：= + - @ 開頭的字串前面加 '，避免在 Excel 被當成公式執行（CSV injection） */
function csvCell(v: unknown): string {
  let s = v === null || v === undefined ? "" : String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 加 BOM：Excel 才會用 UTF-8 開，中文不會變亂碼 */
export function toCsv(rows: unknown[][]): string {
  return "﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

export function download(filename: string, content: string, mime: string) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 用隱藏的 iframe 列印（不開新視窗，不會被擋彈出視窗） */
export function printHtml(html: string) {
  const frame = document.createElement("iframe");
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
  frame.srcdoc = html;
  frame.onload = () => {
    frame.contentWindow?.focus();
    frame.contentWindow?.print();
    setTimeout(() => frame.remove(), 60_000);
  };
  document.body.appendChild(frame);
}

/** 圖片轉成 data URL 內嵌進報告（同源、帶 cookie）；失敗就不放圖 */
export async function imageDataUrl(url: string): Promise<string | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const blob = await res.blob();
    return await new Promise((resolve) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result));
      r.onerror = () => resolve(null);
      r.readAsDataURL(blob);
    });
  } catch {
    return null;
  }
}

export const stamp = (d = new Date()) => {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
};

export const localTime = (d = new Date()) =>
  d.toLocaleString("zh-TW", { hour12: false, dateStyle: "medium", timeStyle: "short" });

/** 檔名不能有的字元換掉 */
export const safeName = (s: string) => s.replace(/[\\/:*?"<>|\s]+/g, "_").slice(0, 60);

// ---------------------------------------------------------------- 報告版面
const LEVEL_NOTE: Record<string, string> = {
  內部: "內部資料：僅限公司內部使用，請勿外流",
  機密: "機密資料：僅限有權限的人員閱讀，請勿複製、轉寄或外流",
};

/** 報告外框：標題、產生時間與身分、機密等級（內部、機密每頁頁首都印）、本機產生的說明 */
export function reportPage(opts: {
  title: string;
  subtitle?: string;
  level: string;
  who: string;
  body: string;
}): string {
  const note = LEVEL_NOTE[opts.level];
  return `<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(opts.title)}</title>
<style>
  :root { color-scheme: light; --ink:#1d1d1f; --mute:#6e6e73; --line:#d2d2d7; --soft:#f5f5f7; --accent:#0066cc; --danger:#b3261e; }
  * { box-sizing: border-box; }
  body { margin: 0 auto; max-width: 860px; padding: 32px 24px 48px; font: 15px/1.7 -apple-system, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif; color: var(--ink); background: #fff; }
  h1 { font-size: 26px; line-height: 1.25; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 28px 0 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  h3 { font-size: 15px; margin: 18px 0 6px; }
  .meta, .fine { color: var(--mute); font-size: 12px; }
  .banner { margin: 0 0 16px; padding: 8px 12px; border: 1.5px solid var(--danger); color: var(--danger); font-weight: 600; font-size: 13px; border-radius: 6px; }
  .q { font-weight: 600; margin: 0 0 6px; }
  .a { white-space: pre-wrap; margin: 0 0 8px; }
  sup a { color: var(--accent); text-decoration: none; font-weight: 600; }
  ol.src { margin: 6px 0 0; padding-left: 0; list-style: none; }
  ol.src li { margin: 0 0 8px; padding: 8px 10px; background: var(--soft); border-radius: 6px; font-size: 13px; }
  ol.src li b { color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }
  th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
  th { background: var(--soft); font-weight: 600; }
  td.diff { background: #fff4e5; }
  .grp td { background: var(--soft); font-weight: 600; }
  img.photo { max-width: 220px; max-height: 220px; border-radius: 6px; border: 1px solid var(--line); }
  .row { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
  a { color: var(--accent); }
  @media print {
    body { padding: 0; max-width: none; }
    h2 { break-after: avoid; }
    ol.src li, tr { break-inside: avoid; }
    .level-print { position: fixed; top: 0; right: 0; font-size: 10px; color: var(--danger); }
  }
</style></head><body>
${note ? `<div class="level-print">${esc(opts.level)}</div><p class="banner">${esc(opts.level)}｜${esc(note)}</p>` : ""}
<h1>${esc(opts.title)}</h1>
${opts.subtitle ? `<p class="meta">${esc(opts.subtitle)}</p>` : ""}
<p class="meta">產生時間 ${esc(localTime())}・匯出身分 ${esc(opts.who)}・本機產生，外送 0 bytes</p>
${opts.body}
<p class="fine" style="margin-top:32px">本報告由地端系統依知識庫與本地模型的輸出整理，內容是草稿，使用前請人工核對；
引用段落依原授權標示，CC BY 4.0 段落請保留標示文字。</p>
</body></html>`;
}

/** 回答裡的 [編號] 換成連到出處清單的上標 */
export function citeLinks(text: string, anchor: string): string {
  return esc(text).replace(/\[(\d+)\]/g, (_, n) => `<sup><a href="#${anchor}-${n}">[${n}]</a></sup>`);
}

export interface ReportSource {
  ref: number;
  title: string;
  topic: string;
  text: string;
  source_url: string | null;
  source_label?: string | null;
  license?: string | null;
  attribution?: string | null;
}

export function sourcesHtml(sources: ReportSource[], anchor: string): string {
  if (!sources.length) return "";
  return `<ol class="src">${sources
    .map(
      (s) => `<li id="${anchor}-${s.ref}"><b>[${s.ref}]</b> 〈${esc(s.title)}〉${esc(s.topic)}<br>${esc(s.text)}<br>
<span class="fine">${
        s.source_url ? `出處：<a href="${esc(s.source_url)}">${esc(s.source_url)}</a>` : `出處：${esc(s.source_label ?? "知識庫")}`
      }${s.license ? `・授權 ${esc(s.license)}` : ""}${s.attribution ? `・${esc(s.attribution)}` : ""}</span></li>`,
    )
    .join("")}</ol>`;
}
