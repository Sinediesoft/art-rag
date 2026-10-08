import { vi } from "vitest";
import type { Account, AccountsResponse, RouteResponse } from "../api/client";

/**
 * 可控制的 mock transport：取代 fetch，依「方法＋路徑」回應。不連後端、Jev、LLM 或雲端。
 * SSE 回應可以一次給完（sse）或由測試一段一段推（liveSse），用來測串流中、完成、停止與錯誤。
 */
export type Handler = (req: { method: string; path: string; body: unknown; signal?: AbortSignal | null; headers: Headers }) => Response | Promise<Response>;

export interface Transport {
  calls: { method: string; path: string; body: unknown; headers: Headers }[];
  on: (method: string, path: string | RegExp, h: Handler) => void;
}

export function mockTransport(): Transport {
  const routes: { method: string; path: string | RegExp; h: Handler }[] = [];
  const calls: Transport["calls"] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.pathname.replace(/^\/api\/v1/, "") + url.search;
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body ?? null;
    const headers = new Headers(init?.headers);
    calls.push({ method, path, body, headers });
    if (init?.signal?.aborted) throw new DOMException("aborted", "AbortError");
    // 後註冊的優先：測試可以覆寫預設回應
    const r = [...routes].reverse().find((x) => x.method === method && (typeof x.path === "string" ? x.path === path.split("?")[0] : x.path.test(path)));
    if (!r) return json({ error: { code: "NOT_MOCKED", message: `${method} ${path}`, request_id: "" } }, 404);
    return r.h({ method, path, body, signal: init?.signal, headers });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, on: (method, path, h) => routes.push({ method, path, h }) };
}

export const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
export const apiError = (status: number, code: string, message = code) => json({ error: { code, message, request_id: `req-${code}` } }, status);

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** 一次給完的 SSE */
export function sse(events: [string, unknown][]) {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(c) {
        for (const [e, d] of events) c.enqueue(enc.encode(frame(e, d)));
        c.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

/** 由測試控制的 SSE：push 一個事件、close 結束；請求被 abort 時串流跟著中斷 */
export function liveSse() {
  const enc = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  let closed = false;
  const stream = new ReadableStream<Uint8Array>({ start: (c) => void (ctrl = c) });
  return {
    respond: (signal?: AbortSignal | null) => {
      signal?.addEventListener("abort", () => {
        if (closed) return;
        closed = true;
        ctrl.error(new DOMException("aborted", "AbortError"));
      });
      return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
    },
    push: (event: string, data: unknown) => !closed && ctrl.enqueue(enc.encode(frame(event, data))),
    close: () => {
      if (closed) return;
      closed = true;
      ctrl.close();
    },
  };
}

export const account = (id = "planner", over: Partial<Account> = {}): Account => ({
  id,
  label: id === "guest" ? "訪客" : id === "planner" ? "生管" : id === "manager" ? "主管" : id,
  role: id,
  role_label: id,
  ops: [],
  warehouses: [],
  customers: [],
  note: "",
  domains: id === "guest" ? ["art"] : ["art", "mfg", "factory"],
  levels: ["公開", "內部", "機密"],
  scope_note: "",
  dept: "生管",
  depts: ["生管"],
  clearance: id === "guest" ? 0 : 2,
  views: [],
  ...over,
});

export const accounts = (current = "planner", demo = true): AccountsResponse => ({
  current: account(current),
  accounts: [account("guest"), account("planner"), account("manager")],
  demo_controls: demo,
  pending_approvals: 0,
  token: {
    claims: { sub: current, roles: [current], clearance: 2 },
    expires_at: "2026-10-08T18:00:00+08:00",
    via: "cookie",
    alg: "HS256",
    unsigned: "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwbGFubmVyIn0",
    checks: [],
  } as unknown as AccountsResponse["token"],
});

/** /agent/route 的回應（預設：第 1、2 段通過、交給畫作問答） */
export function route(over: Partial<RouteResponse> & { dispatch?: Record<string, unknown> } = {}): RouteResponse {
  const base: RouteResponse = {
    request_id: "req-route",
    account: account("planner"),
    question: "梵谷畫這幅畫的時候在哪裡？",
    pii: [],
    masked_text: "[作品A]畫的時候在哪裡？",
    mapping: { "[作品A]": { label: "有絲柏的麥田" } },
    entities: [{ kind: "artwork", id: "met-436535" }],
    photo: null,
    router: { engine: "local", model: "bge-m3", latency_ms: 4, detail: {} },
    intent: "art_qa",
    intent_label: "畫作問答",
    risk: "read",
    confidence: 0.91,
    margin: 0.4,
    ranked: [{ intent: "art_qa", label: "畫作問答", prob: 0.91 } as RouteResponse["ranked"][number]],
    modify_op: null,
    flags: {},
    gate: "direct",
    threshold: 0.5,
    gate_reason: "信心夠高",
    options: [],
    auth: {
      passed: true,
      pending: false,
      checks: [{ key: "role", label: "功能授權", by: "地端", ok: true, detail: "可以", warn: false }],
      tag: null,
      reason: null,
      retry: null,
      filter: { domain: "art", domain_label: "畫作", clearance: 2, depts: [], levels: ["公開"], doc_id: "met-436535", doc_label: "有絲柏的麥田", doc_level: "公開", text: "level ∈ {公開}" },
      degraded: false,
      token: { claims: { name: "生管", roles: ["生管"], clearance: 2 }, expires_at: "", via: "cookie", alg: "HS256", unsigned: "secret-unsigned", checks: [] } as unknown as RouteResponse["auth"]["token"],
    },
    guard: { passed: true, engine: "local", verdict: "query", checks: [], tag: null, reason: null, skipped: null, fallback_reason: null, call: null },
    outcome: "pass",
    blocked: null,
    short_circuit: null,
    dispatch: { module: "art_qa", artwork_id: "met-436535", artwork_label: "有絲柏的麥田", part_id: null, question: "梵谷畫這幅畫的時候在哪裡？" },
    post_filter: "local",
    route_ticket: "ticket-must-not-be-stored",
    egress: { bytes: 0, to: null, images: 0 },
    latency_ms: {},
  };
  return { ...base, ...over, dispatch: { ...base.dispatch, ...(over.dispatch ?? {}) } } as RouteResponse;
}

/** 工廠：圖紙問答（第 1 段確認看得到〈連接法蘭〉） */
export const factoryRoute = (over: Partial<RouteResponse> = {}) =>
  route({
    question: "連接法蘭有哪些公差要求？",
    intent: "drawing_qa",
    intent_label: "圖紙／製程問答",
    dispatch: { module: "drawing_qa", artwork_id: null, artwork_label: undefined, part_id: "mfg-002", part_label: "連接法蘭", question: "連接法蘭有哪些公差要求？" },
    ...over,
    auth: {
      ...route().auth,
      filter: { domain: "mfg", domain_label: "工廠圖紙", clearance: 2, depts: ["生管"], levels: ["公開", "內部", "機密"], doc_id: "mfg-002", doc_label: "連接法蘭", doc_level: "機密", text: "level ∈ {公開,內部,機密}" },
      ...(over.auth ?? {}),
    },
  });

export const sources = (level = "公開", over: Record<string, unknown> = {}) => ({
  request_id: "req-chat",
  artwork_id: "met-436535",
  strategy: "hybrid",
  use_retrieval: true,
  candidates: 3,
  sources: [{ ref: 1, chunk_id: "c1", level, title: "有絲柏的麥田", topic: "創作背景", text: "1889 年在聖雷米。", source_url: "https://example.org", license: "CC0", score: 0.8 }],
  filter: { text: "level ∈ {公開}" },
  post_filter: {
    mode: "local",
    engine: "local",
    candidates: 3,
    kept: 1,
    flagged: [{ chunk_id: "leak", title: "觀眾留言", topic: "洩密段落", by: "地端" }],
    dropped: [],
    cloud: 0,
    local: 3,
    verify: { engine: "local", checks: [], call: null, fallback_reason: null, ms: 1 },
    rerank: { engine: "local", checks: [{ key: "c1", label: "c1", by: "地端", ok: true, detail: "2 分", warn: false }], call: null, fallback_reason: null, ms: 1 },
    gate: { engine: "local", checks: [], call: null, fallback_reason: null, ms: 1, passed: true, message: null as string | null },
    egress_bytes: 0,
    ms: 3,
  },
  pipeline: [
    { stage: 1, name: "認證與授權", status: "pass", by: "地端", detail: "" },
    { stage: 2, name: "Jev Choice", status: "pass", by: "地端", detail: "" },
    { stage: 3, name: "Metadata Filter", status: "pass", by: "地端", detail: "" },
    { stage: 4, name: "Jev Noul", status: "pass", by: "地端", detail: "" },
    { stage: 5, name: "Jev Score", status: "pass", by: "地端", detail: "" },
    { stage: 6, name: "生成閘門", status: "pass", by: "地端", detail: "" },
  ],
  ...over,
});

export const done = (over: Record<string, unknown> = {}) => ({
  request_id: "req-chat",
  strategy_requested: "hybrid",
  strategy_used: "hybrid",
  model: "qwen3-vl:4b",
  fallback: false,
  fallback_reason: null,
  prompt_version: "answer_v4",
  use_retrieval: true,
  latency_ms: { retrieval: 10, first_token: 200, generation: 500, total: 700 },
  tokens: { input: 100, output: 20 },
  cost_twd: 0,
  egress: { images: 0, chunks: 0, bytes: 0 },
  ...over,
});

/** 每個畫面都會打的背景請求（身分、狀態） */
export function baseHandlers(t: Transport, current = "planner") {
  t.on("GET", "/auth/accounts", () => json(accounts(current)));
  t.on("GET", "/status", () =>
    json({ status: "ok", outage_simulated: false, demo_controls: true, demo_warning: null, eval_controls: false, strategies: {}, memory: null, system1: { jev_configured: false, detail: "沒有金鑰", model: "", timeout_s: 1.5 } }),
  );
}
