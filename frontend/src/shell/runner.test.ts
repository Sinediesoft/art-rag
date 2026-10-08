import { describe, expect, it } from "vitest";
import { apiError, done, factoryRoute, json, liveSse, mockTransport, route, sources, sse } from "../test/transport";
import { ctaOf, domainOf } from "./outputs";
import { runTurn } from "./runner";
import { buildStages, progressOf } from "./stages";
import type { ChatPart, SqlPart, Turn } from "./types";

const turn = (text = "梵谷畫這幅畫的時候在哪裡？"): Turn => ({
  id: "t1",
  text,
  imageId: null,
  forced: null,
  at: "entry",
  ts: 1,
  account: null,
  phase: "routing",
  route: null,
  failure: null,
  part: null,
});

/** 跑一輪，回傳「目前狀態」的讀取函式（runTurn 一邊跑一邊更新） */
function start(t: Turn, opts: { signal?: AbortSignal; token?: string } = {}) {
  let state = t;
  const ctrl = new AbortController();
  const p = runTurn(t, (f) => (state = f(state)), { signal: opts.signal ?? ctrl.signal, token: opts.token });
  return { get: () => state, done: p, abort: () => ctrl.abort() };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const until = async (f: () => boolean) => {
  for (let i = 0; i < 200 && !f(); i++) await tick();
  expect(f()).toBe(true);
};

describe("runner：/agent/route → 分派到的模組（全部來自 mock transport）", () => {
  it("文字問答：串流中 → 逐字 → 完成；交接票只用在 /chat 請求", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => json(route()));
    const live = liveSse();
    t.on("POST", "/chat", ({ signal }) => live.respond(signal));
    const run = start(turn());
    await until(() => run.get().part?.kind === "chat");
    expect(run.get().phase).toBe("running");
    const chatCall = () => t.calls.find((c) => c.path === "/chat");
    await until(() => !!chatCall());
    expect((chatCall()!.body as { route_ticket: string }).route_ticket).toBe("ticket-must-not-be-stored");
    live.push("sources", sources());
    await until(() => (run.get().part as ChatPart).status === "streaming");
    live.push("token", { text: "1889 年" });
    live.push("token", { text: "在聖雷米 [1]。" });
    await until(() => (run.get().part as ChatPart).text === "1889 年在聖雷米 [1]。");
    expect(run.get().phase).toBe("running");
    live.push("done", done({ pipeline: [...sources().pipeline, { stage: 7, name: "本地 LLM", status: "pass", by: "地端", detail: "" }] }));
    live.close();
    await run.done;
    const s = run.get();
    expect(s.phase).toBe("done");
    expect((s.part as ChatPart).status).toBe("done");
    const stages = buildStages(s.route!, progressOf(s.part, s.phase));
    expect(stages.map((x) => x.short)).toEqual(["認證授權", "Jev Choice", "Metadata Filter", "Jev Noul", "Jev Score", "生成閘門", "本地 LLM"]);
    expect(stages.every((x) => x.state === "ok" || x.state === "warn")).toBe(true);
    expect(ctaOf(s)?.domain).toBe("art");
  });

  it("拒絕：第 1 段擋下 → 不呼叫任何模組、不給跳轉；後面幾段是「沒有執行」不是通過", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () =>
      json(
        route({
          outcome: "blocked_auth",
          intent: "data_query",
          blocked: { stage: 1, rule: "權限不足", log_no: "SEC-0001", judge: "地端", reason: "訪客不能查工廠資料庫", degraded: false },
          auth: { ...route().auth, passed: false, reason: "訪客不能查工廠資料庫" },
          guard: null,
        }),
      ),
    );
    const run = start(turn("法蘭還剩幾件可以出貨？"));
    await run.done;
    const s = run.get();
    expect(s.phase).toBe("done");
    expect(s.part?.kind).toBe("route");
    expect(t.calls.some((c) => c.path.startsWith("/inventory") || c.path === "/chat")).toBe(false);
    expect(domainOf(s)).toBeNull();
    expect(ctaOf(s)).toBeNull();
    const stages = buildStages(s.route!, {});
    expect(stages[0].state).toBe("block");
    expect(stages.slice(1).every((x) => x.state === "skip")).toBe(true);
  });

  it("401（過期）：重新取得憑證後重送一次", async () => {
    const t = mockTransport();
    let n = 0;
    t.on("POST", "/agent/route", () => (++n === 1 ? apiError(401, "TOKEN_EXPIRED") : json(route({ outcome: "short_circuit", short_circuit: { stage: 2, by: "地端", reply: "我可以幫你找畫。" } }))));
    t.on("GET", "/auth/accounts", () => json({}));
    const run = start(turn("今天天氣如何？"));
    await run.done;
    expect(t.calls.map((c) => c.path)).toEqual(["/agent/route", "/auth/accounts", "/agent/route"]);
    expect(run.get().phase).toBe("done");
    expect(run.get().route?.short_circuit?.reply).toBe("我可以幫你找畫。");
  });

  it("401（竄改的憑證）：不重送，停在閘道拒絕", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => apiError(401, "TOKEN_INVALID", "簽章不符"));
    const run = start(turn(), { token: "forged.jwt.sig" });
    await run.done;
    expect(t.calls.filter((c) => c.path === "/agent/route")).toHaveLength(1);
    expect(t.calls[0].headers.get("Authorization")).toBe("Bearer forged.jwt.sig");
    expect(run.get().phase).toBe("error");
    expect(run.get().failure).toMatchObject({ status: 401, code: "TOKEN_INVALID" });
    expect(domainOf(run.get())).toBeNull();
  });

  it("403：顯示權限錯誤", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => apiError(403, "FORBIDDEN", "沒有權限"));
    const run = start(turn());
    await run.done;
    expect(run.get().failure).toMatchObject({ status: 403, code: "FORBIDDEN" });
  });

  it("連不上伺服器 → 錯誤；重試成功", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => {
      throw new TypeError("Failed to fetch");
    });
    const run = start(turn());
    await run.done;
    expect(run.get().failure).toMatchObject({ status: 0, code: "NETWORK_ERROR" });
    t.on("POST", "/agent/route", () => json(route({ intent: "art_search", dispatch: { artwork_id: null, question: "水邊撐陽傘的人群" } })));
    t.on("GET", /^\/search\/text/, () => json({ query: "q", latency_ms: 3, results: [] }));
    const retry = start(run.get());
    await retry.done;
    expect(retry.get().phase).toBe("done");
    expect(retry.get().failure).toBeNull();
  });

  it("停止：串流中 abort → 已停止，保留已收到的字", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => json(route()));
    const live = liveSse();
    t.on("POST", "/chat", ({ signal }) => live.respond(signal));
    const run = start(turn());
    await until(() => !!t.calls.find((c) => c.path === "/chat"));
    live.push("sources", sources());
    live.push("token", { text: "1889" });
    await until(() => (run.get().part as ChatPart).text === "1889");
    run.abort();
    await run.done;
    expect(run.get().phase).toBe("stopped");
    expect((run.get().part as ChatPart).status).toBe("stopped");
    expect((run.get().part as ChatPart).text).toBe("1889");
  });

  it("停止：還在第 1、2 段就停", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", ({ signal }) => new Promise((_, rej) => signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")))));
    const run = start(turn());
    await tick();
    run.abort();
    await run.done;
    expect(run.get().phase).toBe("stopped");
    expect(run.get().route).toBeNull();
  });

  it("SSE 錯誤事件 → 這一輪是錯誤（可重試），不給跳轉", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => json(factoryRoute()));
    t.on("POST", "/chat", () => sse([["error", { code: "STRATEGY_UNAVAILABLE", message: "推論伺服器離線", request_id: "r9" }]]));
    const run = start(turn("連接法蘭有哪些公差要求？"));
    await run.done;
    expect(run.get().phase).toBe("error");
    expect((run.get().part as ChatPart).error?.code).toBe("STRATEGY_UNAVAILABLE");
    expect(ctaOf(run.get())).toBeNull();
  });

  it("降級：第 6 段生成閘門沒過 → 查無資料，不給模組跳轉", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => json(factoryRoute()));
    const src = sources("機密");
    src.post_filter.gate = { ...src.post_filter.gate, passed: false, message: "查無資料" };
    src.pipeline = [...src.pipeline.slice(0, 5), { stage: 6, name: "生成閘門", status: "block", by: "地端", detail: "查無資料" }];
    t.on("POST", "/chat", () => sse([["sources", src], ["token", { text: "查無資料" }], ["done", done({ degraded: true })]]));
    const run = start(turn("連接法蘭有哪些公差要求？"));
    await run.done;
    const s = run.get();
    expect(domainOf(s)).toBeNull();
    expect(ctaOf(s)).toBeNull();
    const stages = buildStages(s.route!, progressOf(s.part, s.phase));
    expect(stages.find((x) => x.key === "gate")!.state).toBe("block");
    expect(stages.find((x) => x.key === "gen")!.state).toBe("skip");
  });

  it("庫存 Text-to-SQL：SQL、結果表、回答都來自 SSE", async () => {
    const t = mockTransport();
    t.on("POST", "/agent/route", () => json(route({ intent: "data_query", intent_label: "庫存・訂單・工單查詢", dispatch: { artwork_id: null, question: "法蘭還剩幾件可以出貨？" } })));
    t.on("POST", "/inventory/ask", () =>
      sse([
        ["attempt", { n: 1, previous_error: null }],
        ["sql", { attempt: 1, sql: "SELECT 1", ok: true, error: null }],
        ["result", { columns: ["倉庫", "可用"], rows: [["一廠成品倉", 12]], row_count: 1, truncated: false, exec_ms: 2 }],
        ["token", { text: "還有 12 件。" }],
        ["done", { request_id: "r" }],
      ]),
    );
    const run = start(turn("法蘭還剩幾件可以出貨？"));
    await run.done;
    const p = run.get().part as SqlPart;
    expect(p.status).toBe("done");
    expect(p.result?.rows).toEqual([["一廠成品倉", 12]]);
    expect(p.answer).toBe("還有 12 件。");
  });
});
