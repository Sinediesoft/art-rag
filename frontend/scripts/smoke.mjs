// 瀏覽器 smoke test：用 Chrome DevTools Protocol 操作真的頁面（不另外裝套件），連本機後端（建議 mock 模式，不連 Jev、LLM、雲端）。
//
//   node frontend/scripts/smoke.mjs <網址> <輸出資料夾> [寬] [高] [圖片檔]
//   例：node frontend/scripts/smoke.mjs http://127.0.0.1:8010 /tmp/smoke 1440 900 kb/images/met-436535.jpg
//
// 流程：入口新對話 → 工廠簡介 → 進入工廠模組 → 圖紙 → 3D → 庫存 → 排程 → 切回舊成果 → 回入口開同一紀錄；
// 藝術簡介 → 進入藝術模組 → 作品 → 細看 → 年表 → 相似作品（並排比較）；附圖問答、停止、權限拒絕、降級、
// 不持久化憑證。每一步截圖，最後印出每項檢查的結果（有失敗時 exit 1）。Chrome 路徑可用 CHROME 環境變數指定。
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const [base, outDir, w = "1440", h = "900", image] = process.argv.slice(2);
if (!base || !outDir) {
  console.error("用法：node smoke.mjs <網址> <輸出資料夾> [寬] [高] [圖片檔]");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const CHROME =
  process.env.CHROME ??
  (process.platform === "darwin"
    ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    : process.platform === "win32"
      ? "C:/Program Files/Google/Chrome/Application/chrome.exe"
      : "google-chrome");
const port = 9300 + Math.floor(Math.random() * 500);
const mobile = +w < 768;
const chrome = spawn(CHROME, [
  "--headless=new",
  "--use-angle=swiftshader",
  "--enable-unsafe-swiftshader",
  "--hide-scrollbars",
  "--no-first-run",
  `--remote-debugging-port=${port}`,
  `--window-size=${w},${h}`,
  `--user-data-dir=${join(outDir, `.profile-${w}`)}`,
  "about:blank",
]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try {
    target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page");
  } catch {
    /* Chrome 還沒起來 */
  }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
const errors = [];
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) pending.get(m.id)(m), pending.delete(m.id);
  if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text);
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) =>
  new Promise((r) => {
    const i = ++id;
    pending.set(i, r);
    ws.send(JSON.stringify({ id: i, method, params }));
  });
const js = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description ?? r.result.exceptionDetails.text);
  return r.result.result.value;
};
await send("Runtime.enable");
await send("Page.enable");
await send("DOM.enable");
await send("Emulation.setDeviceMetricsOverride", { width: +w, height: +h, deviceScaleFactor: 1, mobile });

const results = [];
let n = 0;
const check = async (name, expr) => {
  let ok = false;
  let detail = "";
  try {
    const v = await js(expr);
    ok = v === true || (typeof v === "object" && v?.ok === true);
    detail = typeof v === "object" ? JSON.stringify(v) : String(v);
  } catch (e) {
    detail = String(e.message ?? e);
  }
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  → ${detail}`}`);
  return ok;
};
const shot = async (name) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, `${w}x${h}-${String(++n).padStart(2, "0")}-${name}.png`), Buffer.from(r.result.data, "base64"));
};
const waitFor = async (expr, ms = 20000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try {
      if (await js(expr)) return true;
    } catch {
      /* 頁面換頁中 */
    }
    await sleep(250);
  }
  return false;
};
const goto = async (path) => {
  await send("Page.navigate", { url: base + path });
  await waitFor("document.readyState === 'complete' && !!document.querySelector('.app')", 15000);
  await sleep(400);
};
/** 點包含文字的按鈕或連結（最後一個符合的，通常是最新一輪） */
const click = (text, scope = "button, a") =>
  js(`(() => { const t = ${JSON.stringify(text)}; const el = [...document.querySelectorAll(${JSON.stringify(scope)})].reverse().find((b) => b.offsetParent !== null && b.textContent.trim().includes(t)); if (!el) return "NOT FOUND: " + t; el.click(); return "clicked"; })()`);
const lastAi = "[...document.querySelectorAll('.msg--ai')].at(-1)";
const idle = `(() => { const m = ${lastAi}; return !!m && !['routing','running'].includes(m.dataset.phase); })()`;
/** 在目前畫面的輸入框輸入並送出；送出後等這一輪結束，遇到「請你選」就點第一個符合的選項 */
const ask = async (text, prefer = "") => {
  // 和使用者一樣：身分確認、輸入框解鎖後才送
  await waitFor("!document.querySelector('.composer__lock')", 15000);
  const before = await js("document.querySelectorAll('.msg--ai').length");
  await js(
    `(() => { const ta = [...document.querySelectorAll(".composer__input")].at(-1); const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set; set.call(ta, ${JSON.stringify(text)}); ta.dispatchEvent(new Event("input", { bubbles: true })); return new Promise((ok) => setTimeout(() => { ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); ok(true); }, 80)); })()`,
  );
  await waitFor(`document.querySelectorAll('.msg--ai').length > ${before}`, 8000);
  await waitFor(idle, 30000);
  const clar = await js(`!!${lastAi}.querySelector('.choices')`);
  if (clar) {
    await js(
      `(() => { const bs = [...${lastAi}.querySelectorAll('.choice')]; const b = bs.find((x) => x.textContent.includes(${JSON.stringify(prefer)})) ?? bs[0]; b?.click(); return b?.textContent; })()`,
    );
    await waitFor(`document.querySelectorAll('.msg--ai').length > ${before + 1}`, 8000);
    await waitFor(idle, 30000);
  }
};
const noOverflow = "document.documentElement.scrollWidth <= innerWidth + 1 && document.body.scrollWidth <= innerWidth + 1";
const composerVisible = `(() => { const c = [...document.querySelectorAll('.composer')].at(-1); if (!c) return false; const r = c.getBoundingClientRect(); return r.bottom <= innerHeight + 1 && r.top >= 0 && r.height > 0; })()`;
const showcaseKind = "(document.querySelector('.showcase__kind')?.textContent ?? '')";
const showPane = async () => {
  if (mobile) await click("展示區", ".mtabs button");
  await sleep(300);
};
const chatPane = async () => {
  if (mobile) await click("對話", ".mtabs button");
  await sleep(200);
};
const switchTo = async (label) => {
  await js(
    `(() => { const b = document.querySelector('.topbar__account') ?? [...document.querySelectorAll('.mtop__btn')].find((x) => x.getAttribute('aria-expanded') !== null && !x.textContent.includes('功能頁')); b?.click(); return true; })()`,
  );
  await sleep(300);
  await click(label, ".menu__item");
  await waitFor(`(document.querySelector('.topbar__account, .mtop__actions')?.textContent ?? '').includes(${JSON.stringify(label)})`, 8000);
  await sleep(300);
};

try {
  // ------------------------------------------------------------ 入口
  await goto("/");
  await waitFor("!!document.querySelector('.home')");
  await check("入口：首頁有問候、建議問句與輸入框", "!!document.querySelector('.home__title') && document.querySelectorAll('.task').length > 0 && !!document.querySelector('.composer__input')");
  await check(mobile ? "入口：手機有開抽屜的按鈕（沒有常駐側欄）" : "入口：桌機有左側對話紀錄欄", mobile ? "!!document.querySelector('[aria-label=\"打開對話紀錄\"]') && !document.querySelector('.side')" : "!!document.querySelector('.side')");
  await check("入口：沒有 Jev／地端守門的手動切換", "!/僅本機|雲端 Jev\\s*只收|第 2、4 道檢查由誰判斷/.test(document.body.innerText) && !document.querySelector('.segmented[role=radiogroup] [aria-checked]')");
  await check("入口：預設深色主題", "document.documentElement.dataset.theme === 'dark' && document.documentElement.dataset.view === 'entry'");
  await shot("entry-home");
  await switchTo("生管");

  // ------------------------------------------------------------ 工廠
  await ask("連接法蘭有哪些公差要求？", "圖紙");
  await check("工廠簡介：七段軌跡（名稱與順序）", `(() => { const s = [...${lastAi}.querySelectorAll('.trace__stage .trace__label')].map((x) => x.textContent); return { ok: s.length === 7 && s[0] === '認證授權' && s[1] === 'Jev Choice' && s[2] === 'Metadata Filter', s }; })()`);
  await check("工廠簡介：圖紙縮圖＋「進入工廠模組」", `!!${lastAi}.querySelector('.subject--drawing img') && (${lastAi}.querySelector('.jump--factory')?.textContent ?? '').includes('進入工廠模組')`);
  await check("工廠簡介：回答來自問答 SSE（有逐字回答或查無資料）", `!!${lastAi}.querySelector('.prose, .answer__note')`);
  await shot("factory-brief");
  await js(`${lastAi}.querySelector('.jump--factory').click()`);
  await waitFor("location.pathname.startsWith('/factory/') && !!document.querySelector('.showcase')", 6000);
  await sleep(600);
  await check("工廠模組：延續同一段對話（入口的那一輪還在）", "document.querySelectorAll('.msg--user').length >= 1 && (document.querySelector('.msg--user .msg__bubble')?.textContent ?? '').includes('連接法蘭')");
  await check("工廠模組：工廠主題（10 Night＋信號紅）", "document.documentElement.dataset.view === 'factory' && getComputedStyle(document.querySelector('.app--module')).getPropertyValue('--mod').trim().toLowerCase() === '#e5533d'");
  if (!mobile) {
    await check("工廠模組：左右各半（--split 50%）", "document.querySelector('.mbody').style.getPropertyValue('--split') === '50%'");
    await js("(() => { const s = document.querySelector('.msplit'); s.focus(); s.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); return true; })()");
    await check("分隔線：方向鍵調寬", "document.querySelector('.msplit').getAttribute('aria-valuenow') === '52'");
    await js("document.querySelector('.msplit').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
    await check("分隔線：雙擊回到 50%", "document.querySelector('.msplit').getAttribute('aria-valuenow') === '50'");
  }
  await showPane();
  await waitFor("!!document.querySelector('.zp__img')", 8000);
  await check("右側：圖紙（真實 /parts 圖檔，可縮放）", `${showcaseKind}.includes('圖紙') && !!document.querySelector('.zp__img') && document.querySelector('.zp__img').naturalWidth > 0 && !!document.querySelector('.zp__tools')`);
  await js("(() => { const z = document.querySelector('.zp'); const r = z.getBoundingClientRect(); z.dispatchEvent(new WheelEvent('wheel', { deltaY: -400, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, bubbles: true })); return true; })()");
  await sleep(300);
  await check("圖紙：滾輪放大", "(document.querySelector('.zp__pct')?.textContent ?? '') !== '100%'");
  await shot("factory-drawing");
  await chatPane();

  await ask("把這張圖轉成 3D", "3D");
  await check("3D：問句帶入正在看的對象（〈連接法蘭〉）", `(${lastAi}.previousElementSibling?.textContent ?? '').includes('連接法蘭')`);
  await check("3D：確認卡（不自動執行）", `!!${lastAi}.querySelector('.taskcard') && (${lastAi}.textContent).includes('開始轉換')`);
  await click("開始轉換", ".taskcard button");
  const built = await waitFor("[...document.querySelectorAll('.strip__item')].some((b) => b.textContent.includes('3D'))", 150000);
  await showPane();
  if (built) await waitFor("!!document.querySelector('.view__canvas--model canvas')", 15000);
  await check("右側：3D 模型（Ortho2CAD STL，可旋轉縮放的 three.js 畫布）", `${showcaseKind}.includes('3D') && !!document.querySelector('.view__canvas--model canvas')`);
  await shot("factory-3d");
  await chatPane();

  await ask("法蘭還剩幾件可以出貨？", "庫存");
  await showPane();
  await waitFor("[...document.querySelectorAll('.strip__item')].some((b) => /庫存|查詢/.test(b.textContent))", 15000);
  await check("右側：庫存／唯讀 SQL 結果表", `/庫存|查詢/.test(${showcaseKind}) && !!document.querySelector('.view--data table')`);
  await shot("factory-stock");
  await chatPane();

  await ask("重新排程", "排程");
  await showPane();
  await check("右側：目前排程（/production/overview）", `${showcaseKind}.includes('排程')`);
  await chatPane();
  await click("開始排程", ".taskcard button");
  await waitFor("[...document.querySelectorAll('.strip__item')].filter((b) => b.textContent.includes('排程')).length >= 2", 60000);
  await showPane();
  await waitFor("!!document.querySelector('.gantt-wrap svg')", 8000);
  await check("右側：排程結果甘特圖（Timefold／簡易排程的解）", `${showcaseKind}.includes('排程') && !!document.querySelector('.gantt-wrap svg') && (document.querySelector('.showcase__title')?.textContent ?? '').includes('重新排程')`);
  await shot("factory-schedule");

  const count = await js("document.querySelectorAll('.strip__item').length");
  await js("document.querySelector('.strip__item').click()");
  await sleep(400);
  await check("縮圖歷程：點第 1 張切回圖紙，之前的成果都在", `${showcaseKind}.includes('圖紙') && document.querySelectorAll('.strip__item').length === ${count}`);
  await js("(() => { const s = document.querySelector('.showcase'); s.focus(); s.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); return true; })()");
  await sleep(300);
  await check("縮圖歷程：→ 切到下一張", "document.querySelectorAll('.strip__item')[1]?.classList.contains('is-on')");
  await js("(() => { const s = document.querySelector('.showcase'); s.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })); return true; })()");
  await sleep(300);
  await check("縮圖歷程：← 切回", `${showcaseKind}.includes('圖紙')`);
  await check("工廠模組：沒有水平溢出", noOverflow);
  await chatPane();
  await check("工廠模組：輸入列在畫面內、沒有被遮住", composerVisible);
  await showPane();
  const factoryUrl = await js("location.pathname");

  await chatPane();
  await js("document.querySelector('.mtop__back').click()");
  await waitFor("location.pathname.startsWith('/c/')", 5000);
  await check("回入口：同一段對話還在（入口外觀）", "document.documentElement.dataset.view === 'entry' && document.querySelectorAll('.msg--user').length >= 4");
  // 新對話（不重新整理頁面）→ 從對話紀錄打開剛才那一段
  if (mobile) {
    await js("document.querySelector('[aria-label=\"新對話\"]').click()");
    await waitFor("location.pathname === '/'", 3000);
    await js("document.querySelector('[aria-label=\"打開對話紀錄\"]').click()");
    await sleep(300);
    await check("手機：對話紀錄抽屜（新對話、搜尋、分組、刪除）", "!!document.querySelector('.drawer .side--drawer .side__new') && !!document.querySelector('.drawer .side__search input') && !!document.querySelector('.drawer .side__when') && !!document.querySelector('.drawer .side__del')");
  } else {
    await js("document.querySelector('.side__new').click()");
    await waitFor("location.pathname === '/'", 3000);
    await check("桌機：對話紀錄欄（新對話、搜尋、分組、刪除）", "!!document.querySelector('.side__search input') && !!document.querySelector('.side__when') && !!document.querySelector('.side__del')");
    await js("document.querySelector('.side__head .side__icon').click()");
    await sleep(200);
    await check("桌機：對話紀錄欄可以收合成窄欄", "!!document.querySelector('.side.is-mini')");
    await js("document.querySelector('.side.is-mini .side__icon').click()");
    await sleep(200);
  }
  await js("document.querySelector('.side__open').click()");
  await waitFor(`location.pathname === ${JSON.stringify(factoryUrl)}`, 5000);
  await showPane();
  await check("開啟紀錄：回到工廠模組、當時的成果歷程與目前成果（圖紙）", `location.pathname === ${JSON.stringify(factoryUrl)} && document.querySelectorAll('.strip__item').length === ${count} && ${showcaseKind}.includes('圖紙')`);
  await shot("factory-reopen");
  // 重新整理：工廠內部資料沒有存進瀏覽器，只照編號以目前身分重新讀取；查詢結果要重新查詢
  await goto(factoryUrl);
  await showPane();
  await waitFor("!!document.querySelector('.zp__img, .view__error')", 8000);
  await check(
    "重新整理後：回到同一個模組與成果（圖紙依編號重新讀取、查詢結果要重新查詢）",
    `location.pathname === ${JSON.stringify(factoryUrl)} && ${showcaseKind}.includes('圖紙') && !!document.querySelector('.zp__img') && document.querySelectorAll('.strip__item').length >= 3`,
  );
  await js("[...document.querySelectorAll('.strip__item')].find((b) => /查詢|庫存/.test(b.textContent))?.click()");
  await sleep(300);
  await check("重新整理後：查詢結果沒有存進瀏覽器，提供「以目前身分重新查詢」", "(document.querySelector('.showcase')?.textContent ?? '').includes('以目前身分重新查詢')");
  await js("[...document.querySelectorAll('.strip__item')].find((b) => b.textContent.includes('3D'))?.click()");
  await waitFor("!!document.querySelector('.view__canvas--model canvas')", 15000);
  await check("重新整理後：3D 依工作編號重新讀取，畫布可以旋轉縮放", `${showcaseKind}.includes('3D') && !!document.querySelector('.view__canvas--model canvas')`);
  await shot("factory-reload");

  // ------------------------------------------------------------ 藝術
  await goto("/");
  await ask("梵谷畫這幅畫的時候在哪裡？", "畫作");
  await check("藝術簡介：畫作縮圖＋「進入藝術模組」", `!!${lastAi}.querySelector('.subject:not(.subject--drawing) img') && (${lastAi}.querySelector('.jump--art')?.textContent ?? '').includes('進入藝術模組')`);
  await shot("art-brief");
  await js(`${lastAi}.querySelector('.jump--art').click()`);
  await waitFor("location.pathname.startsWith('/art/') && !!document.querySelector('.showcase')", 6000);
  await sleep(500);
  await check("藝術模組：暖褐金箔主題", "document.documentElement.dataset.view === 'art' && getComputedStyle(document.querySelector('.app--module')).getPropertyValue('--mod').trim().toLowerCase() === '#c9a45c' && getComputedStyle(document.body).backgroundColor === 'rgb(15, 12, 9)'");
  await showPane();
  await waitFor("!!document.querySelector('.zp__img')", 8000);
  await check("右側：作品大圖＋展品標籤", `${showcaseKind}.includes('作品') && !!document.querySelector('.museum-label') && document.querySelector('.zp__img').naturalWidth > 0`);
  await shot("art-artwork");
  await chatPane();
  await ask("細看這幅畫的技法", "畫作");
  await showPane();
  await waitFor(`${showcaseKind}.includes('細看')`, 8000);
  await check("右側：細看（段落＋色彩分析，可縮放）", `${showcaseKind}.includes('細看') && document.querySelectorAll('.notes .note').length > 0 && !!document.querySelector('.zp__img')`);
  await shot("art-detail");
  await chatPane();
  await ask("這幅畫的畫家當時在哪裡？", "畫作");
  await showPane();
  await check("右側：年表（館藏依年代，本作標出）", `[...document.querySelectorAll('.strip__item')].some((b) => b.textContent.includes('年表')) && (document.querySelector('.strip__item.is-on')?.textContent.includes('年表') ? !!document.querySelector('.tl__item.is-focus') : true)`);
  await js("[...document.querySelectorAll('.strip__item')].find((b) => b.textContent.includes('年表'))?.click()");
  await waitFor("!!document.querySelector('.tl__item.is-focus')", 8000);
  await shot("art-timeline");
  await chatPane();
  await ask("有沒有和這幅畫風格相近的作品？", "畫作");
  await showPane();
  await waitFor("!!document.querySelector('.sim-list, .wall, .view--similar .empty')", 10000);
  await check("右側：相似作品排序", `${showcaseKind}.includes('相似作品') && !!document.querySelector('.view--similar')`);
  const canCompare = await js("!!document.querySelector('.sim .btn--primary')");
  if (canCompare) {
    await js("document.querySelector('.sim .btn--primary').click()");
    await waitFor("!!document.querySelector('.compare__table')", 8000);
    await check("相似作品：並排比較（/compare/items）", "!!document.querySelector('.compare__table tr')");
  }
  await shot("art-similar");
  await check("藝術模組：沒有水平溢出", noOverflow);

  // ------------------------------------------------------------ 附圖、停止、拒絕、降級、儲存
  await goto("/");
  if (image) {
    const doc = await send("DOM.getDocument", {});
    const q = await send("DOM.querySelector", { nodeId: doc.result.root.nodeId, selector: "input[type=file]" });
    await send("DOM.setFileInputFiles", { nodeId: q.result.nodeId, files: [resolve(image)] });
    await waitFor("!!document.querySelector('.composer__photo img')", 15000);
    await check("附圖：上傳後預覽（伺服器網址，不是本機 blob）", "(document.querySelector('.composer__photo img')?.src ?? '').includes('/api/v1/images/')");
    await ask("這幅畫畫的是哪裡？", "畫作");
    await check("附圖問答：照片在本機辨識並分派", `!!${lastAi}.parentElement.querySelector('.msg__photo') && /照片在本機辨識|畫作/.test(${lastAi}.textContent + ${lastAi}.querySelector('.trace')?.title)`);
    await shot("photo");
    await goto("/");
  }
  await js(
    `(() => { const ta = document.querySelector(".composer__input"); const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set; set.call(ta, "介紹谿山行旅圖"); ta.dispatchEvent(new Event("input", { bubbles: true })); ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); return true; })()`,
  );
  await waitFor("!!document.querySelector('.composer__send.is-stop')", 3000);
  await js("document.querySelector('.composer__send.is-stop')?.click()");
  await waitFor(idle, 10000);
  await check("停止：這一輪停在「已停止」或已經完成", `['stopped','done'].includes(${lastAi}.dataset.phase)`);

  await switchTo("訪客");
  await goto("/");
  await ask("法蘭還剩幾件可以出貨？");
  await check("權限拒絕：訪客查工廠資料庫 → 第 1 段擋下、沒有跳轉按鈕", `!!${lastAi}.querySelector('.alertcard') && !!${lastAi}.querySelector('.trace__stage.is-block') && !${lastAi}.querySelector('.jump')`);
  await shot("blocked");
  await switchTo("業務・甲");
  await goto("/");
  await ask("連接法蘭有哪些公差要求？", "圖紙");
  await check("降級：業務問機密圖紙 → 查無資料，不給圖紙按鈕或縮圖", `/查無資料/.test(${lastAi}.textContent) && !${lastAi}.querySelector('.jump') && !${lastAi}.querySelector('.subject--drawing')`);
  await shot("degraded");

  await check(
    "瀏覽器儲存：沒有 JWT、交接票、Jev 請求本文、內部資料",
    `(() => { const s = Object.keys(localStorage).map((k) => localStorage.getItem(k)).join(""); const bad = [/eyJ[\\w-]+\\.[\\w-]+/, /route_ticket/, /"request"\\s*:/, /blob:/, /"claims"/, /"rows"\\s*:/].filter((r) => r.test(s)).map(String); return { ok: s.length > 0 && bad.length === 0 && !/公差/.test(JSON.stringify(JSON.parse(localStorage.getItem('artrag-shell-v1')).convs.flatMap((c) => c.turns.map((t) => t.archived.answer ?? '')))), bad, size: s.length }; })()`,
  );
  await check("cookie：JWT 是 HttpOnly（document.cookie 讀不到）", "!/eyJ/.test(document.cookie)");
} catch (e) {
  results.push({ name: "執行", ok: false, detail: String(e.stack ?? e) });
  console.log("ERROR " + (e.stack ?? e));
}

const failed = results.filter((r) => !r.ok);
writeFileSync(join(outDir, `${w}x${h}-results.json`), JSON.stringify({ base, size: `${w}x${h}`, results, pageErrors: errors }, null, 2));
console.log(`\n${results.length - failed.length}/${results.length} 通過（${w}×${h}）`);
if (errors.length) console.log("PAGE ERRORS:\n" + [...new Set(errors)].slice(0, 20).join("\n"));
ws.close();
chrome.kill();
process.exit(failed.length ? 1 : 0);
