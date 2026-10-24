// 開發者平台「07 背景工作」的行為測試：用 jsdom 載入 frontend/public/developer-workbench.html，
// fetch 換成可控制的假後端（不連真的後端、Redis、worker）。這頁是單一 HTML，不在 vitest／tsc 的範圍，所以獨立用 node:test 跑：
//
//   node --test frontend/scripts/developer-workbench-jobs.test.mjs
//
// 測：內嵌 JavaScript 語法、正常／空清單、503／未啟用／離線、服務狀況分辨、事件解析與 Last-Event-ID 續接去重、輪詢備援、
// 切換工作與離頁的過期回應隔離、換帳號、撤權清除、取消的各種結果與晚到的取消回應、清單 401／403 立即清除、
// 產物連結、清單只在需要時自動更新。
// 另外放了幾個不屬於 07、但要同一套假後端的（Codex 審查 2026-10-24）：憑證過期或別的分頁換了身分時撤下舊身分的資料、
// 照片建檔儲存中鎖住欄位。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import { JSDOM, VirtualConsole } from "jsdom";

const HTML = readFileSync(new URL("../public/developer-workbench.html", import.meta.url), "utf8");
const CAD = "cad_0123456789abcdef";
const CAD2 = "cad_fedcba9876543210";
const SCH = "sch_0123456789abcdef";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const enc = new TextEncoder();

async function waitFor(fn, what, ms = 3000) {
  const t0 = Date.now();
  for (;;) {
    let v;
    try {
      v = fn();
    } catch {
      v = undefined;
    }
    if (v) return v;
    if (Date.now() - t0 > ms) throw new Error(`等不到：${what}`);
    await sleep(5);
  }
}

// ---------------------------------------------------------------- 假後端
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const apiError = (status, code, message = code) => json({ error: { code, message, request_id: "req-test" } }, status);
const frame = (ev, data, id) => `${id ? `id: ${id}\n` : ""}event: ${ev}\ndata: ${JSON.stringify(data)}\n\n`;
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
};

/** 一次給完的 SSE：chunks 是原始文字（可以故意切在事件中間） */
function sseRaw(chunks) {
  return new Response(
    new ReadableStream({
      start(c) {
        for (const s of chunks) c.enqueue(enc.encode(s));
        c.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}
const sse = (events) => sseRaw(events.map(([ev, data, id]) => frame(ev, data, id)));

/** 由測試一段一段推的 SSE；頁面中止請求時串流跟著中斷 */
function liveSse(call) {
  let ctrl;
  let closed = false;
  const stream = new ReadableStream({ start: (c) => void (ctrl = c) });
  call.signal?.addEventListener("abort", () => {
    if (closed) return;
    closed = true;
    ctrl.error(new DOMException("aborted", "AbortError"));
  });
  return {
    res: new Response(stream, { headers: { "Content-Type": "text/event-stream" } }),
    push: (ev, data, id) => !closed && ctrl.enqueue(enc.encode(frame(ev, data, id))),
    close: () => {
      if (closed) return;
      closed = true;
      ctrl.close();
    },
    get aborted() {
      return !!call.signal?.aborted;
    },
  };
}

const account = (id) => ({ id, label: { warehouse: "倉管", manager: "主管", guest: "一般員工" }[id], views: id === "manager" ? ["diagnostics"] : [], ops: [] });
const svcOk = (over = {}) => ({
  enabled: true,
  database_ok: true,
  broker_ok: true,
  pending_dispatch: 0,
  counts: { succeeded: 2, running: 1 },
  workers: [
    { name: "gpu@pc", queues: ["gpu"], pid: 1, age_s: 3 },
    { name: "schedule@pc", queues: ["schedule"], pid: 2, age_s: 4 },
  ],
  ...over,
});
const job = (over = {}) => {
  const id = over.id ?? CAD;
  return {
    id,
    kind: id.startsWith("sch_") ? "schedule" : "cad",
    status: "running",
    owner_id: "warehouse",
    attempt: 1,
    max_attempts: 3,
    cancel_requested: false,
    stop_requested: false,
    progress: null,
    result: null,
    error: null,
    input_version: null,
    params: { strategy: "ortho2cad" },
    target: { part_id: "mfg-002" },
    created_at: "2026-10-23T06:00:00+00:00",
    started_at: "2026-10-23T06:00:05+00:00",
    finished_at: null,
    last_event_id: 2,
    status_url: `/api/v1/jobs/${id}`,
    events_url: `/api/v1/jobs/${id}/events`,
    artifacts: [],
    ...over,
  };
};

function backend({ current = "warehouse", jobs = [], svc = svcOk() } = {}) {
  const routes = [];
  const calls = [];
  const state = { current };
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input), "http://127.0.0.1:5173");
    const method = (init.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "") + url.search;
    const call = { method, path, headers: new Headers(init.headers ?? {}), signal: init.signal };
    calls.push(call);
    if (init.signal?.aborted) throw new DOMException("aborted", "AbortError");
    const r = [...routes].reverse().find((x) => x.method === method && (typeof x.path === "string" ? x.path === path : x.path.test(path)));
    if (!r) return apiError(404, "NOT_MOCKED", `${method} ${path}`);
    return r.h(call);
  };
  const be = { fetch, calls, state, on: (method, path, h) => routes.push({ method, path, h }) };
  be.on("GET", "/auth/accounts", () => json({ current: account(state.current), accounts: [account("guest"), account("warehouse"), account("manager")], demo_controls: true }));
  be.on("GET", "/status", () => json({ status: "ok", strategies: {}, system1: { jev_configured: false, detail: "" }, memory: null, chat_defaults: {}, jobs: svc }));
  be.on("GET", "/jobs?limit=50", () => json({ items: jobs }));
  for (const j of jobs) be.on("GET", `/jobs/${j.id}`, () => json(j));
  return be;
}
const callsTo = (be, method, prefix) => be.calls.filter((c) => c.method === method && c.path.startsWith(prefix));
const eventCalls = (be, id) => be.calls.filter((c) => c.path === `/jobs/${id}/events`);

// ---------------------------------------------------------------- 頁面
async function openPage(be, liveCfg = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(String(e?.stack ?? e)));
  vc.on("error", (...a) => errors.push(a.join(" ")));
  const dom = new JSDOM(HTML, {
    url: "http://127.0.0.1:5173/developer-workbench.html",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(win) {
      win.fetch = be.fetch;
      win.TextDecoder = TextDecoder;
      win.TextEncoder = TextEncoder;
      win.matchMedia = (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
      if (!win.queueMicrotask) win.queueMicrotask = queueMicrotask;
    },
  });
  const win = dom.window;
  const p = {
    win,
    errors,
    $: (s) => win.document.querySelector(s),
    $$: (s) => [...win.document.querySelectorAll(s)],
    ev: (code) => win.eval(code),
    text: (s) => win.document.querySelector(s)?.textContent ?? "",
    click: (s) => {
      const el = win.document.querySelector(s);
      if (!el) throw new Error(`找不到元素：${s}`);
      el.click();
    },
    clickText: (text, scope = "button") => {
      const el = [...win.document.querySelectorAll(scope)].find((b) => b.textContent.includes(text));
      if (!el) throw new Error(`找不到按鈕：${text}`);
      el.click();
    },
    close: () => win.close(),
  };
  // 測試用短間隔：重連退讓、輪詢、清單自動更新
  p.ev("Object.assign(JOB_CFG,{retryMs:[15,15,15],maxFailures:3,pollMs:20,pollStreamEvery:1000,listMs:60})");
  // 定時對身分預設 10 秒，其他測試跑完前不會觸發；要測它的測試另外給短間隔（連上之後才開始計時，在這裡改來得及）
  p.ev(`Object.assign(LIVE_CFG,${JSON.stringify(liveCfg)})`);
  await waitFor(() => ["live", "offline"].includes(p.ev("LIVE.state")), "連線狀態");
  return p;
}
async function gotoJobs(p) {
  p.click('[data-act="go"][data-arg="jobs"]');
  await waitFor(() => p.$("#jobList"), "07 頁");
}
async function withPage(be, fn, liveCfg) {
  const p = await openPage(be, liveCfg);
  try {
    await fn(p);
    assert.deepEqual(p.errors, [], "頁面不應有 JavaScript 錯誤");
  } finally {
    p.close();
  }
}

// ---------------------------------------------------------------- 測試
test("內嵌 JavaScript 語法正確", () => {
  const scripts = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.equal(scripts.length, 1);
  assert.doesNotThrow(() => new vm.Script(scripts[0], { filename: "developer-workbench.inline.js" }));
});

test("正常清單：真資料列表、選取第一筆、完整詳情、事件、產物連結；佇列 CAD→gpu、排程→schedule；Worker／相關請求寫未提供", async () => {
  const done = job({
    status: "succeeded",
    finished_at: "2026-10-23T06:02:00+00:00",
    progress: { stage: "executing", label: "沙箱執行中" },
    result: { meta: { strategy: "ortho2cad", model: "ortho2cad-8b", part: { part_no: "FLG-2002", name_zh: "連接法蘭" } }, result: { ok: true, valid: true, dims: { width: 10, depth: 20, height: 5 }, iou: 0.91 }, done: { latency_ms: { total: 61000 } } },
    artifacts: [
      { name: "model.stl", url: `/api/v1/jobs/${CAD}/artifacts/model.stl`, content_type: "model/stl", size_bytes: 2048 },
      { name: "evil", url: "javascript:alert(1)", content_type: "text/html", size_bytes: 1 },
    ],
  });
  const be = backend({ jobs: [done, job({ id: SCH, kind: "schedule", status: "queued", params: { seconds: 20, engine: "timefold" }, target: null, started_at: null })] });
  be.on("GET", `/jobs/${CAD}/events`, () => sse([["created", { status: "queued", at: "2026-10-23T06:00:00+00:00" }, 1], ["started", { attempt: 1, resource: "gpu:0" }, 2], ["succeeded", { status: "succeeded" }, 3]]));
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.$$("#jobList tr.click").length === 2, "兩列工作");
    const rows = p.$$("#jobList tr.click").map((r) => r.textContent);
    assert.match(rows[0], /3D 重建.*完成.*未提供.*1\/3.*gpu/);
    assert.match(rows[1], /排程.*排隊.*未提供.*0?1\/3.*schedule/);
    await waitFor(() => p.text("#jobFollow").includes("工作已結束"), "終態後串流關閉");
    const d = p.text("#jobDetail");
    for (const s of [CAD, "第 1 次，最多 3 次", "建立者warehouse", "Worker未提供", "相關請求未提供", "FLG-2002 連接法蘭", "10 × 20 × 5 mm", "沒有「重試」"]) assert.ok(d.includes(s), `詳情應有「${s}」：${d}`);
    assert.equal(p.$$("#jobEvents li").length, 3);
    assert.ok(p.$(`#jobDetail a[href="/api/v1/jobs/${CAD}/artifacts/model.stl"]`), "開啟產物的連結");
    assert.ok(p.$(`#jobDetail a[href="/api/v1/jobs/${CAD}/artifacts/model.stl?download=true"][download]`), "下載產物的連結");
    assert.ok(!p.$$("#jobDetail a").some((a) => a.getAttribute("href").startsWith("javascript:")), "不是這個工作產物網址的連結不畫");
    assert.ok(!p.$$("#jobDetail button").some((b) => b.textContent.trim() === "重試"), "沒有手動重試按鈕");
    assert.ok(!/J-20\d\d/.test(p.text("main")), "不混入模擬的工作");
    await sleep(120);
    assert.equal(eventCalls(be, CAD).length, 1, "已結束的串流不重連");
  });
});

test("空清單：顯示空狀態，說明不是故障；服務狀況照 /status 顯示正常", async () => {
  const be = backend({ jobs: [] });
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobList").includes("0 個"), "空狀態");
    assert.ok(p.text("#jobList").includes("這不是故障"));
    assert.ok(p.text("#jobDetail").includes("沒有工作可以看"));
    assert.ok(p.text("#jobSvc").includes("gpu worker（3D 重建）正常gpu@pc"));
    assert.equal(be.calls.filter((c) => c.path.includes("/events")).length, 0);
  });
});

test("503 未啟用：寫出原因與重試入口，不顯示假的工作；重試成功後換成真清單", async () => {
  const be = backend({ svc: { enabled: false, database_ok: false, broker_ok: false, pending_dispatch: 0, counts: {}, workers: [] } });
  be.on("GET", "/jobs?limit=50", () => apiError(503, "JOBS_UNAVAILABLE", "這台伺服器沒有啟用背景工作系統（REDIS_URL、JOBS_DATABASE_URL）"));
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobList").includes("背景工作系統無法使用"), "503 訊息");
    assert.ok(p.text("#jobList").includes("REDIS_URL"));
    assert.ok(p.text("#jobSvc").includes("未啟用"));
    assert.ok(!/J-20\d\d/.test(p.text("main")));
    assert.equal(p.$$("#jobList tr.click").length, 0);
    be.on("GET", "/jobs?limit=50", () => json({ items: [job({ status: "succeeded" })] }));
    be.on("GET", `/jobs/${CAD}`, () => json(job({ status: "succeeded" })));
    be.on("GET", `/jobs/${CAD}/events`, () => sse([["succeeded", {}, 1]]));
    p.clickText("重試", "#jobList button");
    await waitFor(() => p.$$("#jobList tr.click").length === 1, "重試後的清單");
  });
});

test("服務狀況分得出：資料庫不通、Redis 不通、沒有對應佇列的 worker", async () => {
  const be = backend({ jobs: [], svc: svcOk({ database_ok: false, broker_ok: false, pending_dispatch: 2, workers: [{ name: "schedule@pc", queues: ["schedule"], pid: 2, age_s: 1 }] }) });
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobList").includes("0 個"), "清單讀完");
    const s = p.text("#jobSvc");
    assert.match(s, /工作資料庫（PostgreSQL）連不上/);
    assert.match(s, /Redis（broker）連不上.*待投遞 2 個/);
    assert.match(s, /gpu worker（3D 重建）注意沒有 worker 在線/);
    assert.match(s, /schedule worker（排程）正常/);
    assert.ok(p.text("#jobList").includes("服務狀況有異常"), "空清單同時提醒服務有狀況");
  });
});

test("後端離線：07 寫連不上與重新連線入口，不用模擬資料；重新連線成功後讀真清單", async () => {
  const be = backend({ jobs: [] });
  let down = true;
  const real = be.fetch;
  const page = await openPage({
    ...be,
    fetch: (input, init) => {
      if (down) return Promise.reject(new TypeError("Failed to fetch"));
      return real(input, init);
    },
  });
  try {
    assert.equal(page.ev("LIVE.state"), "offline");
    page.click('[data-act="go"][data-arg="jobs"]');
    await waitFor(() => page.text("main").includes("連不上後端"), "離線訊息");
    assert.ok(page.text("main").includes("沒有模擬版本"));
    assert.ok(!/J-20\d\d/.test(page.text("main")));
    down = false;
    page.clickText("重新連線");
    await waitFor(() => page.text("#jobList").includes("0 個"), "重新連線後的清單");
    assert.deepEqual(page.errors, []);
  } finally {
    page.close();
  }
});

test("事件串流：解析 retry／註解／切開的事件；斷線帶 Last-Event-ID 續接，重複的事件不重複顯示；終態後讀最終結果", async () => {
  const be = backend({ jobs: [job()] });
  const lastIds = [];
  be.on("GET", `/jobs/${CAD}/events`, (call) => {
    lastIds.push(call.headers.get("Last-Event-ID"));
    if (lastIds.length === 1) {
      const half = frame("started", { attempt: 1, resource: "gpu:0" }, 2);
      return sseRaw(["retry: 2000\n\n: keepalive\n\n", frame("created", { status: "queued" }, 1), half.slice(0, 15), half.slice(15)]);
    }
    // 故意把 #2 再送一次：頁面要用 id 去重
    return sse([["started", { attempt: 1 }, 2], ["progress", { stage: "generating", label: "模型生成中", chars: 120 }, 3], ["succeeded", { status: "succeeded" }, 4]]);
  });
  let gets = 0;
  be.on("GET", `/jobs/${CAD}`, () => json(++gets === 1 ? job() : job({ status: "succeeded", finished_at: "2026-10-23T06:03:00+00:00", artifacts: [{ name: "model.step", url: `/api/v1/jobs/${CAD}/artifacts/model.step`, content_type: "application/step", size_bytes: 4096 }] })));
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobFollow").includes("工作已結束"), "終態");
    const seqs = p.$$("#jobEvents li .seq").map((x) => x.textContent);
    assert.deepEqual(seqs, ["#1", "#2", "#3", "#4"]);
    assert.ok(p.text("#jobEvents").includes("模型生成中：120 字元"));
    assert.ok(p.text("#jobDetail").includes("model.step"), "終態後讀到的產物");
    await sleep(100);
    assert.deepEqual(lastIds, [null, "2"], "第一次從頭讀；斷線後從最後收到的 #2 之後接，終態之後不再重連");
  });
});

test("串流一直連不上：退讓重連幾次後改用狀態輪詢，到終態時顯示最終狀態與錯誤", async () => {
  const be = backend({ jobs: [job()] });
  be.on("GET", `/jobs/${CAD}/events`, () => apiError(503, "JOBS_UNAVAILABLE", "背景工作系統暫時無法使用"));
  let polls = 0;
  be.on("GET", `/jobs/${CAD}`, () => json(++polls < 6 ? job() : job({ status: "failed", finished_at: "2026-10-23T06:04:00+00:00", error: { code: "STRATEGY_UNAVAILABLE", message: "推論伺服器無法使用" } })));
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.ev("JOBS.f?.state") === "polling", "改用輪詢");
    assert.ok(p.text("#jobFollow").includes("改用狀態輪詢"));
    await waitFor(() => p.text("#jobFollow").includes("工作已結束"), "輪詢到終態");
    assert.ok(p.text("#jobDetail").includes("推論伺服器無法使用（STRATEGY_UNAVAILABLE）"));
    assert.ok(p.text("#jobDetail").includes("失敗"));
    assert.equal(eventCalls(be, CAD).length, 4, "3 次失敗後輪詢；到終態再試一次串流補事件就停");
  });
});

test("切換工作：上一個工作晚到的回應不寫進新工作的詳情", async () => {
  const slow = deferred();
  const be = backend({ jobs: [job(), job({ id: CAD2 })] });
  be.on("GET", `/jobs/${CAD}`, () => slow.promise);
  be.on("GET", `/jobs/${CAD2}/events`, (call) => liveSse(call).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => callsTo(be, "GET", `/jobs/${CAD}`).length === 1, "第一個工作的詳情請求");
    p.click(`#jobList tr[data-arg="${CAD2}"]`);
    await waitFor(() => p.text("#jobFollow").includes("串流連線中"), "第二個工作的串流");
    slow.resolve(json(job({ progress: { stage: "generating", label: "A 的進度" } })));
    await sleep(50);
    assert.equal(p.ev("JOBS.detail.id"), CAD2);
    assert.ok(p.text("#jobDetail").includes(CAD2));
    assert.ok(!p.text("#jobDetail").includes("A 的進度"), "舊工作的回應被丟掉");
    assert.equal(eventCalls(be, CAD).length, 0, "舊工作不開串流");
  });
});

test("離開 07：停止串流與計時器、不取消工作，之後的事件與清單回應都不寫入", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    stream.push("started", { attempt: 1 }, 1);
    await waitFor(() => p.$$("#jobEvents li .seq").length === 1, "第一個事件");
    p.click('[data-act="go"][data-arg="overview"]');
    await waitFor(() => stream.aborted, "串流被中止");
    const lists = callsTo(be, "GET", "/jobs?").length;
    stream.push("progress", { chars: 5 }, 2);
    await sleep(250);
    assert.equal(callsTo(be, "POST", "/jobs/").length, 0, "離開只停止觀察，不呼叫取消");
    assert.equal(callsTo(be, "GET", "/jobs?").length, lists, "離開後清單不再自動更新");
    assert.equal(p.ev("JOBS.events.length"), 0, "離開後不留事件");
    assert.equal(p.ev("JOBS.f"), null);
  });
});

test("換帳號：停掉上一個身分的串流，晚到的舊清單回應不採用，改讀新身分的清單", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  be.on("POST", "/auth/switch", () => {
    be.state.current = "manager";
    return json({ current: account("manager"), accounts: [account("guest"), account("warehouse"), account("manager")] });
  });
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    const oldList = deferred();
    be.on("GET", "/jobs?limit=50", () => oldList.promise);
    p.clickText("重新整理", ".head button");
    await waitFor(() => callsTo(be, "GET", "/jobs?").length === 2, "倉管的第二次清單請求");
    be.on("GET", "/jobs?limit=50", () => json({ items: [job({ id: SCH, kind: "schedule", status: "succeeded", target: null })] }));
    be.on("GET", `/jobs/${SCH}`, () => json(job({ id: SCH, kind: "schedule", status: "succeeded", target: null })));
    be.on("GET", `/jobs/${SCH}/events`, () => sse([["succeeded", {}, 1]]));
    p.click("#liveSwitch");
    await waitFor(() => stream.aborted, "倉管的串流被中止");
    await waitFor(() => p.ev("JOBS.account") === "manager" && p.$$("#jobList tr.click").length === 1, "主管的清單");
    oldList.resolve(json({ items: [job(), job({ id: CAD2 })] }));
    await sleep(50);
    const ids = p.$$("#jobList tr.click").map((r) => r.dataset.arg);
    assert.deepEqual(ids, [SCH], "舊身分晚到的清單不採用");
    assert.equal(p.ev("STATE.jobs.sel"), SCH);
  });
});

test("憑證過期換發成別的身分：07 跟著換成新身分的資料", async () => {
  const be = backend({ jobs: [] });
  let n = 0;
  be.on("GET", "/jobs?limit=50", () => {
    if (++n === 1) {
      be.state.current = "guest";
      return apiError(401, "TOKEN_EXPIRED");
    }
    return json({ items: [] });
  });
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.ev("JOBS.account") === "guest" && p.text("#jobList").includes("一般員工"), "換成一般員工");
    assert.equal(p.ev("LIVE.account.id"), "guest");
  });
});

test("撤權：串流送 PERMISSION_REVOKED 時立刻清掉詳情與事件，清單重讀、不再重連", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    stream.push("progress", { stage: "generating", label: "機密的進度", chars: 9 }, 3);
    await waitFor(() => p.text("#jobEvents").includes("機密的進度"), "事件");
    be.on("GET", "/jobs?limit=50", () => json({ items: [] }));
    const lists = callsTo(be, "GET", "/jobs?").length;
    stream.push("error", { code: "PERMISSION_REVOKED", message: "您已沒有這個工作的讀取權限" });
    await waitFor(() => p.text("#jobDetail").includes("目前身分沒有權限"), "撤權訊息");
    assert.ok(p.text("#jobDetail").includes("已清除"));
    assert.ok(!p.text("main").includes("機密的進度"), "受保護的事件內容不留在畫面上");
    assert.equal(p.ev("JOBS.detail"), null);
    assert.equal(p.ev("JOBS.events.length"), 0);
    await waitFor(() => callsTo(be, "GET", "/jobs?").length > lists, "清單重讀");
    await waitFor(() => p.$$("#jobList tr.click").length === 0, "清單拿掉看不到的工作");
    await sleep(80);
    assert.equal(eventCalls(be, CAD).length, 1, "撤權後不重連");
  });
});

test("詳情回 404：寫出原因、不顯示內容", async () => {
  const be = backend({ jobs: [job()] });
  be.on("GET", `/jobs/${CAD}`, () => apiError(404, "JOB_NOT_FOUND", "找不到這個工作，可能已超過保存期限"));
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobDetail").includes("找不到，或目前身分看不到"), "404 訊息");
    assert.equal(eventCalls(be, CAD).length, 0);
  });
});

async function cancelCase(status, reply, check) {
  const be = backend({ jobs: [job({ status })] });
  be.on("GET", `/jobs/${CAD}/events`, (call) => liveSse(call).res);
  const gate = deferred();
  be.on("POST", `/jobs/${CAD}/cancel`, () => gate.promise);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobFollow").includes("串流連線中"), "串流");
    p.clickText("取消工作", "#jobDetail button");
    p.click('#jobDetail [data-act="cancelJob"]'); // 連按：送出中不重送
    await waitFor(() => p.text("#jobDetail").includes("取消中…"), "送出中");
    gate.resolve(reply());
    await waitFor(() => p.$("#jobDetail .jb-msg"), "取消結果");
    assert.equal(callsTo(be, "POST", `/jobs/${CAD}/cancel`).length, 1, "只送出一次");
    await check(p, be);
  });
}

test("取消：排隊中直接取消", () =>
  cancelCase("queued", () => json({ result: "cancelled", job: job({ status: "cancelled", finished_at: "2026-10-23T06:01:00+00:00" }) }), async (p) => {
    assert.ok(p.text("#jobDetail .jb-msg").includes("已取消：工作還在排隊"));
    assert.ok(p.$('#jobDetail [data-act="cancelJob"]').disabled);
  }));

test("取消：執行中待取消（worker 停下來才算取消）", () =>
  cancelCase("running", () => json({ result: "cancelling", job: job({ cancel_requested: true }) }), async (p) => {
    assert.ok(p.text("#jobDetail .jb-msg").includes("已要求取消：工作執行中"));
    const b = p.$('#jobDetail [data-act="cancelJob"]');
    assert.ok(b.disabled && b.textContent.includes("已要求取消"));
  }));

test("取消：409 已進入寫回，重讀實際狀態", () =>
  cancelCase("running", () => apiError(409, "JOB_COMMITTING", "工作已進入寫回階段，無法取消（會完成並寫入結果）"), async (p, be) => {
    assert.ok(p.text("#jobDetail .jb-msg").includes("寫回階段"));
    const b = p.$('#jobDetail [data-act="cancelJob"]');
    assert.ok(b.disabled && b.textContent.includes("寫回中"));
    await waitFor(() => callsTo(be, "GET", `/jobs/${CAD}`).filter((c) => c.path === `/jobs/${CAD}`).length >= 2, "重讀詳情");
  }));

test("取消：409 已經結束", () =>
  cancelCase("running", () => apiError(409, "JOB_ALREADY_FINISHED", "工作已經結束（succeeded），無法取消"), async (p) => {
    assert.ok(p.text("#jobDetail .jb-msg").includes("JOB_ALREADY_FINISHED"));
  }));

test("取消：403 沒有取消權限", () =>
  cancelCase("running", () => apiError(403, "PERMISSION_DENIED", "目前身分沒有取消這個工作的權限"), async (p) => {
    assert.ok(p.text("#jobDetail .jb-msg").includes("不能取消這個工作（403 PERMISSION_DENIED）"));
    assert.ok(!p.$('#jobDetail [data-act="cancelJob"]').disabled, "沒取消成功，按鈕照常可以按");
  }));

test("取消回應晚到：取消 A 後切到 B 再切回 A，A 已讀到終態，舊的取消回應不能把它蓋回執行中", async () => {
  const be = backend({ jobs: [job(), job({ id: CAD2 })] });
  const gate = deferred();
  be.on("GET", `/jobs/${CAD}/events`, (call) => liveSse(call).res);
  be.on("GET", `/jobs/${CAD2}/events`, (call) => liveSse(call).res);
  be.on("POST", `/jobs/${CAD}/cancel`, () => gate.promise);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    p.ev("JOB_CFG.listMs=100000");
    await waitFor(() => p.text("#jobFollow").includes("串流連線中"), "A 的串流");
    p.click('#jobDetail [data-act="cancelJob"]');
    await waitFor(() => callsTo(be, "POST", `/jobs/${CAD}/cancel`).length === 1, "送出取消");
    p.click(`#jobList tr[data-arg="${CAD2}"]`);
    await waitFor(() => p.ev("JOBS.detail?.id") === CAD2, "切到 B");
    be.on("GET", `/jobs/${CAD}`, () => json(job({ status: "cancelled", finished_at: "2026-10-23T07:00:00+00:00" })));
    be.on("GET", `/jobs/${CAD}/events`, () => sse([["cancelled", { status: "cancelled" }, 9]]));
    p.click(`#jobList tr[data-arg="${CAD}"]`);
    await waitFor(() => p.ev("JOBS.f?.state") === "done", "切回 A，讀到終態");
    gate.resolve(json({ result: "cancelling", job: job({ status: "running", cancel_requested: true }) }));
    await sleep(50);
    assert.equal(p.ev("JOBS.detail.status"), "cancelled", "舊的取消回應作廢");
    assert.ok(!p.$("#jobDetail .jb-msg"), "舊回應的訊息也不顯示");
  });
});

test("取消回應晚到（同一個工作）：串流先送到終態，回應裡較舊的快照不覆蓋，等待中的取消訊息照常顯示", async () => {
  const be = backend({ jobs: [job()] });
  const gate = deferred();
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  be.on("POST", `/jobs/${CAD}/cancel`, () => gate.promise);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    p.ev("JOB_CFG.listMs=100000");
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    p.click('#jobDetail [data-act="cancelJob"]');
    await waitFor(() => callsTo(be, "POST", `/jobs/${CAD}/cancel`).length === 1, "送出取消");
    be.on("GET", `/jobs/${CAD}`, () => json(job({ status: "cancelled", cancel_requested: true, finished_at: "2026-10-23T07:00:00+00:00", last_event_id: 4 })));
    stream.push("cancel_requested", { attempt: 1 }, 3);
    stream.push("cancelled", { status: "cancelled", note: "已取消，沒有寫回" }, 4);
    await waitFor(() => p.ev("JOBS.detail.status") === "cancelled", "串流到終態");
    gate.resolve(json({ result: "cancelling", job: job({ status: "running", cancel_requested: true, last_event_id: 3 }) }));
    await waitFor(() => p.$("#jobDetail .jb-msg"), "取消結果");
    assert.ok(p.text("#jobDetail .jb-msg").includes("已要求取消"), "還在原工作上：取消的回應照常顯示");
    assert.equal(p.ev("JOBS.detail.status"), "cancelled", "較舊的快照不蓋掉終態");
  });
});

for (const [status, code] of [
  [401, "TOKEN_INVALID"],
  [403, "PERMISSION_DENIED"],
]) {
  test(`清單 ${status}：即使原本的串流還開著，也立刻停掉並清除詳情、事件與取消狀態；不自動重打`, async () => {
    const be = backend({ jobs: [job({ progress: { stage: "generating", label: "private-progress" } })] });
    let stream;
    be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
    await withPage(be, async (p) => {
      await gotoJobs(p);
      await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
      stream.push("progress", { stage: "generating", label: "private-progress", chars: 10 }, 3);
      await waitFor(() => p.ev("JOBS.events.length") === 1, "事件");
      be.on("GET", "/jobs?limit=50", () => apiError(status, code, "讀不了背景工作"));
      await p.ev("jobsLoad()");
      assert.equal(p.ev("JOBS.detail"), null, "詳情立刻清掉");
      assert.equal(p.ev("JOBS.events.length"), 0, "事件立刻清掉");
      assert.equal(p.ev("JOBS.f"), null, "訂閱停掉");
      assert.ok(stream.aborted, "原本的串流被中止");
      assert.equal(p.ev("JOBS.list"), null);
      await waitFor(() => p.text("#jobDetail").includes(`回 ${status}`), "詳情卡寫原因");
      assert.ok(!p.text("main").includes("private-progress"), "受保護的內容不留在畫面上");
      assert.ok(p.text("#jobList").includes(`回 ${status}`) && p.$$("#jobList button").some((b) => b.textContent.includes("重試")), "清單寫原因並給重試");
      const lists = callsTo(be, "GET", "/jobs?").length;
      const events = eventCalls(be, CAD).length;
      await sleep(250);
      assert.equal(callsTo(be, "GET", "/jobs?").length, lists, "不遞迴、不定時重打清單");
      assert.equal(eventCalls(be, CAD).length, events, "不重連串流");
    });
  });
}

test("正在看的工作從清單消失（別的分頁換了身分）：重新驗證，404 就清除，即使舊串流還開著", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    p.ev("JOB_CFG.listMs=100000");
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    stream.push("progress", { stage: "generating", label: "private-progress", chars: 1 }, 3);
    await waitFor(() => p.ev("JOBS.events.length") === 1, "事件");
    be.on("GET", "/jobs?limit=50", () => json({ items: [] }));
    be.on("GET", `/jobs/${CAD}`, () => apiError(404, "JOB_NOT_FOUND", "找不到這個工作，可能已超過保存期限"));
    await p.ev("jobsLoad()");
    await waitFor(() => p.ev("JOBS.detail") === null && stream.aborted, "清除並停掉串流");
    assert.ok(!p.text("main").includes("private-progress"));
    assert.ok(p.text("#jobDetail").includes("找不到，或目前身分看不到"));
  });
});

test("清單 404（這台後端沒有 /jobs）不是撤權：寫原因，已在看的工作照常", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    be.on("GET", "/jobs?limit=50", () => apiError(404, "NOT_FOUND", "Not Found"));
    await p.ev("jobsLoad()");
    assert.equal(p.ev("JOBS.detail.id"), CAD);
    assert.ok(!stream.aborted);
    await waitFor(() => p.text("#jobList").includes("剛才更新失敗"), "清單寫原因");
  });
});

test("清單自動更新只在有進行中的工作時才做，而且不會重設事件清單與選取", async () => {
  const be = backend({ jobs: [job()] });
  let stream;
  be.on("GET", `/jobs/${CAD}/events`, (call) => (stream = liveSse(call)).res);
  await withPage(be, async (p) => {
    await gotoJobs(p);
    await waitFor(() => stream && p.text("#jobFollow").includes("串流連線中"), "串流");
    stream.push("started", { attempt: 1 }, 1);
    await waitFor(() => p.$("#jobEvents li .seq"), "事件");
    const first = p.$("#jobEvents li");
    const n = callsTo(be, "GET", "/jobs?").length;
    await waitFor(() => callsTo(be, "GET", "/jobs?").length >= n + 2, "自動更新兩次", 2000);
    assert.equal(p.$("#jobEvents li"), first, "事件清單的節點沒有被重畫");
    assert.ok(p.$(`#jobList tr.sel[data-arg="${CAD}"]`), "選取還在");
  });
  const idle = backend({ jobs: [job({ status: "succeeded" })] });
  idle.on("GET", `/jobs/${CAD}/events`, () => sse([["succeeded", {}, 1]]));
  await withPage(idle, async (p) => {
    await gotoJobs(p);
    await waitFor(() => p.text("#jobFollow").includes("工作已結束"), "終態");
    await sleep(250);
    assert.equal(callsTo(idle, "GET", "/jobs?").length, 1, "全部是終態：不自動更新");
  });
});

test("模擬資料的工作編號（開發助理的連結）不會打到後端", async () => {
  const be = backend({ jobs: [job({ status: "succeeded" })] });
  be.on("GET", `/jobs/${CAD}/events`, () => sse([["succeeded", {}, 1]]));
  await withPage(be, async (p) => {
    p.ev("ACT.openJob('J-2041')");
    await waitFor(() => p.text("#jobDetail").includes("模擬資料裡的編號"), "說明");
    await sleep(50);
    assert.equal(be.calls.filter((c) => c.path.includes("J-2041")).length, 0);
  });
});

// ---------------------------------------------------------------- 07 以外（Codex 審查 2026-10-24）
const diagWith = (question) => ({
  db: true,
  index_consistent: true,
  index_problems: [],
  kb_version: "2026.10.2",
  llm_mode: "mock",
  embed_mode: "mock",
  allow_cloud: false,
  system1: {},
  demo_controls: true,
  eval_controls: false,
  recent_routes: [],
  recent_chats: [{ created_at: "2026-10-24T01:00:00+00:00", request_id: "req-mgr", question, strategy_used: "rag", model: "m", fallback: false, first_token_ms: 1, total_ms: 2, egress_bytes: 0 }],
  recent_sql: [],
});

test("憑證過期換發成一般員工：主管讀到的診斷與請求紀錄立刻撤下，換身分前送出、晚到的主管回應不寫入", async () => {
  const SECRET = "主管才看得到的問題";
  const be = backend({ current: "manager" });
  const lateDiag = deferred();
  let hold = false;
  be.on("GET", "/admin/diagnostics", () => {
    if (be.state.current !== "manager") return apiError(403, "FORBIDDEN");
    return hold ? lateDiag.promise : json(diagWith(SECRET));
  });
  await withPage(be, async (p) => {
    await waitFor(() => p.ev("'recent' in LD"), "主管的請求紀錄");
    p.click('[data-act="go"][data-arg="trace"]');
    await waitFor(() => p.win.document.body.textContent.includes(SECRET), "請求追蹤顯示主管的紀錄");
    // 下一輪重讀：診斷用主管身分送出後卡住；/status 晚一點才發現憑證過期，換發成一般員工
    hold = true;
    let expired = false;
    be.on("GET", "/status", async () => {
      if (expired) return json({ status: "ok", strategies: {}, system1: { jev_configured: false, detail: "" }, memory: null, chat_defaults: {}, jobs: svcOk() });
      expired = true;
      await sleep(10);
      be.state.current = "guest";
      return apiError(401, "TOKEN_EXPIRED");
    });
    p.ev("liveLoad()");
    await waitFor(() => p.ev("LIVE.account.id") === "guest" && p.ev("REAL_ERR.diag?.status") === 403, "換成一般員工並重讀");
    assert.ok(!p.win.document.body.textContent.includes(SECRET), "換身分後畫面上不再有主管的紀錄");
    lateDiag.resolve(json(diagWith(SECRET)));
    await sleep(50);
    assert.equal(p.ev("'diag' in REAL"), false, "舊身分晚到的診斷不寫入");
    assert.equal(p.ev("'recent' in LD"), false);
    assert.ok(!p.win.document.body.textContent.includes(SECRET));
  });
});

test("照片建檔儲存修改：送出到回應之間欄位鎖住，回應回來後恢復可編輯並顯示儲存的值", async () => {
  const fields = (title) => [
    { key: "title_zh", label: "標題", kind: "text", value: title, status: "ok" },
    { key: "artist_zh", label: "作者", kind: "text", value: "舊作者", status: "ok" },
  ];
  const draft = (title) => ({ draft_id: "drf-1", status: "draft", domain: "art", item_id: "art-x", photo_url: "/p.jpg", kb_image_url: null, can_commit: false, checks: [], blockers: [], extraction: null, fields: fields(title) });
  const be = backend({ current: "manager" });
  const put = deferred();
  be.on("PUT", "/intake/drf-1", () => put.promise);
  await withPage(be, async (p) => {
    p.click('[data-act="go"][data-arg="kb"]');
    p.ev(`Object.assign(INTAKE,{open:true,imageId:'img-1',draft:${JSON.stringify(draft("舊標題"))}});render()`);
    const field = (k) => p.$(`#intakeBox [data-key="${k}"]`);
    await waitFor(() => field("title_zh"), "草稿表單");
    field("title_zh").value = "新標題";
    field("title_zh").dispatchEvent(new p.win.Event("input", { bubbles: true }));
    p.click("#intakeSave");
    await waitFor(() => callsTo(be, "PUT", "/intake/").length === 1, "送出儲存");
    assert.ok(p.$$('#intakeBox [data-in="intakeField"]').every((el) => el.disabled), "儲存中每個欄位都不能改");
    put.resolve(json(draft("新標題")));
    await waitFor(() => field("artist_zh") && !field("artist_zh").disabled, "回應後恢復可編輯");
    assert.equal(field("title_zh").value, "新標題");
    assert.deepEqual(JSON.parse(p.ev("JSON.stringify(INTAKE.edits)")), {});
  });
});

// ---------------------------------------------------------------- 07 以外（Codex 第二次審查 2026-10-24）
const SECRET = "主管才看得到的問題";
/** 診斷只開給主管，其他身分 403（和後端一樣） */
const diagForManager = (be) => be.on("GET", "/admin/diagnostics", () => (be.state.current === "manager" ? json(diagWith(SECRET)) : apiError(403, "FORBIDDEN")));
test("別的分頁在主介面換了身分（沒有 401）：按「重新整理」、切回這個分頁都會對出來，舊身分的資料撤下", async () => {
  const be = backend({ current: "manager" });
  diagForManager(be);
  await withPage(be, async (p) => {
    await waitFor(() => p.ev("'recent' in LD"), "主管的請求紀錄");
    be.state.current = "guest";
    p.click('[data-act="refresh"]');
    await waitFor(() => p.ev("LIVE.account.id") === "guest" && p.ev("REAL_ERR.diag?.status") === 403, "重新整理後換成一般員工");
    assert.equal(p.ev("'recent' in LD"), false);
    assert.ok(p.text("#heroSub").includes("一般員工"));
    p.click('[data-act="go"][data-arg="trace"]');
    assert.ok(!p.win.document.body.textContent.includes(SECRET), "畫面上不再有主管的紀錄");
    be.state.current = "manager";
    p.win.document.dispatchEvent(new p.win.Event("visibilitychange"));
    await waitFor(() => p.ev("LIVE.account.id") === "manager" && p.ev("'recent' in LD"), "切回分頁後換成主管");
  });
});

test("別的分頁換了身分：定時對身分（測試縮成 30 毫秒）會跟著換；分頁在背景時不讀", async () => {
  const be = backend({ current: "manager" });
  diagForManager(be);
  let hidden = false;
  await withPage(
    be,
    async (p) => {
      Object.defineProperty(p.win.document, "hidden", { configurable: true, get: () => hidden });
      await waitFor(() => p.ev("'recent' in LD"), "主管的請求紀錄");
      const accountCalls = () => callsTo(be, "GET", "/auth/accounts").length;
      const n0 = accountCalls();
      await waitFor(() => accountCalls() >= n0 + 2, "定時對身分");
      hidden = true;
      await sleep(60);
      const n = accountCalls();
      be.state.current = "guest";
      await sleep(150);
      assert.equal(accountCalls(), n, "分頁在背景時不讀");
      assert.equal(p.ev("LIVE.account.id"), "manager");
      // 不送 visibilitychange：只靠定時檢查對出來
      hidden = false;
      await waitFor(() => p.ev("LIVE.account.id") === "guest" && p.ev("REAL_ERR.diag?.status") === 403, "定時檢查後換成一般員工");
      assert.equal(p.ev("'recent' in LD"), false);
    },
    { recheckMs: 30 },
  );
});
