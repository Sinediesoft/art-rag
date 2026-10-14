import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiError, json, jobSse, liveSse, mockTransport, type Transport } from "../test/transport";
import { clearKeys, stableKey } from "./client";
import { runCadJob, runScheduleJob } from "./jobFlow";
import { followJob } from "./jobs";
import { streamJobEvents } from "./sse";

const JOB = "cad_0123456789abcdef";
const SCH = "sch_0123456789abcdef";

const job = (over: Record<string, unknown> = {}) => ({
  id: JOB,
  kind: "cad",
  status: "succeeded",
  owner_id: "warehouse",
  attempt: 1,
  max_attempts: 3,
  cancel_requested: false,
  stop_requested: false,
  progress: null,
  result: null,
  error: null,
  input_version: null,
  params: null,
  target: { part_id: "mfg-002" },
  created_at: "2026-10-13T00:00:00+00:00",
  started_at: null,
  finished_at: null,
  last_event_id: 5,
  status_url: `/api/v1/jobs/${JOB}`,
  events_url: `/api/v1/jobs/${JOB}/events`,
  artifacts: [],
  ...over,
});

const created = (id = JOB, kind = "cad", over: Record<string, unknown> = {}) =>
  json({ job_id: id, kind, status: "queued", status_url: `/api/v1/jobs/${id}`, events_url: `/api/v1/jobs/${id}/events`, idempotent_replay: false, delayed_dispatch: false, ...over }, 202);

const cadMeta = { request_id: "r", job_id: JOB, strategy: "ortho2cad", model: "m", image_id: null, part: null, identified: null, layout: "kb", input_url: "", input_size: [1, 1], scale_to: null, scale_source: null };
const cadResult = { ok: true, error: null, valid: true, repaired: false, raw_dims: null, dims: { width: 1, depth: 2, height: 3 }, scale: 1, volume: 1, faces: 4, iou: 0.99, iou_bbox: 0.99, files: { "model.stl": `/api/v1/jobs/${JOB}/artifacts/model.stl`, "code.py": `/api/v1/jobs/${JOB}/artifacts/code.py` } };
const cadDone = { request_id: "r", job_id: JOB, strategy: "ortho2cad", model: "m", latency_ms: { first_token: 1, generation: 1, exec: 1, total: 3 }, tokens: { input: 1, output: 1 }, egress: { images: 0, chunks: 0, bytes: 0 } };

let t: Transport;
beforeEach(() => {
  t = mockTransport();
  clearKeys();
});
afterEach(() => {
  vi.unstubAllGlobals();
  clearKeys();
});

const calls = (method: string, prefix: string) => t.calls.filter((c) => c.method === method && c.path.startsWith(prefix));

describe("事件串流（持久化遞增 id、Last-Event-ID 續接）", () => {
  it("解析 id／event／data；after>0 時帶 Last-Event-ID 標頭", async () => {
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["progress", { n: 1 }], ["succeeded", { status: "succeeded" }]], 4));
    const got: [number, string][] = [];
    await streamJobEvents(JOB, 3, (e) => got.push([e.id, e.type]));
    expect(got).toEqual([[4, "progress"], [5, "succeeded"]]);
    expect(t.calls[0].headers.get("Last-Event-ID")).toBe("3");
    await streamJobEvents(JOB, 0, () => undefined);
    expect(t.calls[1].headers.get("Last-Event-ID")).toBeNull();
  });

  it("串流中的 error 事件（撤權、資料庫暫時不可用）丟 ApiError，不當成工作的終態", async () => {
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["error", { code: "PERMISSION_REVOKED", message: "您已沒有這個工作的讀取權限" }]]));
    await expect(streamJobEvents(JOB, 0, () => undefined)).rejects.toMatchObject({ code: "PERMISSION_REVOKED", status: 403 });
  });
});

describe("followJob", () => {
  it("斷線後用最後收到的 id 重連：只收沒收到的事件，不重複套用", async () => {
    let n = 0;
    t.on("GET", `/jobs/${JOB}/events`, ({ headers }) => {
      n++;
      if (n === 1) return jobSse([["created", {}], ["started", { attempt: 1 }]]); // 串流在終態之前就斷了
      expect(headers.get("Last-Event-ID")).toBe("2");
      return jobSse([["progress", { chars: 10 }], ["succeeded", { status: "succeeded" }]], 3);
    });
    t.on("GET", `/jobs/${JOB}`, () => json(job()));
    const seen: number[] = [];
    const final = await followJob(JOB, { onEvent: (e) => seen.push(e.id) }, new AbortController().signal, { retryMs: [1] });
    expect(seen).toEqual([1, 2, 3, 4]);
    expect(final?.status).toBe("succeeded");
    expect(n).toBe(2);
  });

  it("事件串流一直連不上就改用輪詢，仍然拿得到終態", async () => {
    t.on("GET", `/jobs/${JOB}/events`, () => apiError(503, "JOBS_UNAVAILABLE"));
    let polls = 0;
    t.on("GET", `/jobs/${JOB}`, () => json(job({ status: ++polls < 3 ? "running" : "failed", error: { code: "X", message: "壞了" } })));
    const polled: string[] = [];
    const final = await followJob(JOB, { onPoll: (j) => polled.push(j.status) }, new AbortController().signal, { retryMs: [1], pollMs: 1 });
    expect(polled).toEqual(["running", "running", "failed"]);
    expect(final?.status).toBe("failed");
  });

  it("沒有權限或工作不存在：回報後停止，不重試", async () => {
    t.on("GET", `/jobs/${JOB}/events`, () => apiError(404, "JOB_NOT_FOUND"));
    const errors: string[] = [];
    const final = await followJob(JOB, { onError: (e) => errors.push(e.code) }, new AbortController().signal, { retryMs: [1] });
    expect(final).toBeNull();
    expect(errors).toEqual(["JOB_NOT_FOUND"]);
    expect(calls("GET", `/jobs/${JOB}/events`)).toHaveLength(1);
  });

  it("中止只停止訂閱：不呼叫取消，也不再讀工作", async () => {
    const live = liveSse();
    t.on("GET", `/jobs/${JOB}/events`, ({ signal }) => live.respond(signal));
    const ctrl = new AbortController();
    const done = followJob(JOB, {}, ctrl.signal);
    await vi.waitFor(() => expect(calls("GET", `/jobs/${JOB}/events`)).toHaveLength(1));
    live.push("progress", { chars: 1 }, 3);
    ctrl.abort();
    expect(await done).toBeNull();
    expect(t.calls.filter((c) => c.path.includes("/cancel"))).toEqual([]);
    expect(calls("GET", `/jobs/${JOB}`).filter((c) => !c.path.includes("/events"))).toEqual([]);
  });
});

describe("3D 重建流程（runCadJob）", () => {
  const handlers = () => {
    const log: string[] = [];
    return {
      log,
      h: {
        onJob: (id: string) => log.push(`job:${id}`),
        onQueued: () => log.push("queued"),
        onRunning: () => log.push("running"),
        onMeta: () => log.push("meta"),
        onExecuting: () => log.push("executing"),
        onCode: (c: string) => log.push(`code:${c}`),
        onResult: () => log.push("result"),
        onDone: () => log.push("done"),
        onError: (e: { code: string }) => log.push(`error:${e.code}`),
        onCancelled: () => log.push("cancelled"),
      },
    };
  };

  it("建立（202、帶 Idempotency-Key）→ 事件 → 結果與受保護的程式碼產物", async () => {
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/cad/jobs", () => created());
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["started", { attempt: 1 }], ["meta", cadMeta], ["stage", { stage: "executing" }], ["succeeded", {}]]));
    t.on("GET", `/jobs/${JOB}`, () => json(job({ result: { meta: cadMeta, result: cadResult, done: cadDone } })));
    t.on("GET", `/jobs/${JOB}/artifacts/code.py`, () => new Response("CODE_FROM_ARTIFACT"));
    const { log, h } = handlers();
    await runCadJob({ part_id: "mfg-002", strategy: "ortho2cad" }, h, new AbortController().signal);
    expect(log).toEqual([`job:${JOB}`, "queued", "running", "meta", "executing", "code:CODE_FROM_ARTIFACT", "result", "done"]);
    const post = calls("POST", "/cad/jobs")[0];
    expect(post.headers.get("Idempotency-Key")).toMatch(/.{8,}/);
    expect(post.body).toEqual({ part_id: "mfg-002", strategy: "ortho2cad" });
  });

  it("同一個目標已經有進行中的工作：接回它，不重複建立", async () => {
    t.on("GET", "/jobs", () => json({ items: [job({ status: "running", target: { part_id: "mfg-002" } }), job({ id: "cad_ffffffffffffffff", target: { part_id: "mfg-001" } })] }));
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["started", { attempt: 1 }], ["succeeded", {}]]));
    t.on("GET", `/jobs/${JOB}`, () => json(job({ result: { meta: cadMeta, result: cadResult, done: cadDone } })));
    t.on("GET", `/jobs/${JOB}/artifacts/code.py`, () => new Response("C"));
    const { log, h } = handlers();
    await runCadJob({ part_id: "mfg-002" }, h, new AbortController().signal);
    expect(calls("POST", "/cad/jobs")).toHaveLength(0);
    expect(log[0]).toBe(`job:${JOB}`);
  });

  it("沙箱執行失敗的工作（失敗但有結果）：照樣取回程式碼與結果給授權的人看", async () => {
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/cad/jobs", () => created());
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["failed", { status: "failed", code: "CAD_EXEC_FAILED", message: "NameError" }]]));
    const failed = { ...cadResult, ok: false, error: "NameError" };
    t.on("GET", `/jobs/${JOB}`, () => json(job({ status: "failed", result: { meta: cadMeta, result: failed, done: cadDone }, error: { code: "CAD_EXEC_FAILED", message: "NameError" } })));
    t.on("GET", `/jobs/${JOB}/artifacts/code.py`, () => new Response("BAD_CODE"));
    const { log, h } = handlers();
    await runCadJob({ part_id: "mfg-002" }, h, new AbortController().signal);
    expect(log).toContain("code:BAD_CODE");
    expect(log).toContain("result");
  });

  it("失敗而且沒有結果、已取消、建立被拒絕", async () => {
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/cad/jobs", () => created());
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["failed", {}]]));
    t.on("GET", `/jobs/${JOB}`, () => json(job({ status: "failed", error: { code: "STRATEGY_UNAVAILABLE", message: "推論伺服器無法使用" } })));
    let r = handlers();
    await runCadJob({ part_id: "mfg-002" }, r.h, new AbortController().signal);
    expect(r.log).toContain("error:STRATEGY_UNAVAILABLE");

    t.on("GET", `/jobs/${JOB}`, () => json(job({ status: "cancelled" })));
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["cancelled", {}]]));
    r = handlers();
    await runCadJob({ part_id: "mfg-002" }, r.h, new AbortController().signal);
    expect(r.log).toContain("cancelled");

    t.on("POST", "/cad/jobs", () => apiError(503, "JOBS_UNAVAILABLE"));
    r = handlers();
    await runCadJob({ part_id: "mfg-002" }, r.h, new AbortController().signal);
    expect(r.log).toEqual(["error:JOBS_UNAVAILABLE"]);
  });

  it("建立請求結果不確定（連不上）：重送沿用同一個 Idempotency-Key；成功後下一次才換新的", async () => {
    let n = 0;
    const keys: (string | null)[] = [];
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/cad/jobs", ({ headers }) => {
      keys.push(headers.get("Idempotency-Key"));
      return ++n === 1 ? new Response(null, { status: 502 }) : created();
    });
    t.on("GET", `/jobs/${JOB}/events`, () => jobSse([["created", {}], ["cancelled", {}]]));
    t.on("GET", `/jobs/${JOB}`, () => json(job({ status: "cancelled" })));
    await runCadJob({ part_id: "mfg-002" }, handlers().h, new AbortController().signal);
    await runCadJob({ part_id: "mfg-002" }, handlers().h, new AbortController().signal);
    await runCadJob({ part_id: "mfg-002" }, handlers().h, new AbortController().signal);
    expect(keys[0]).toBe(keys[1]); // 502 之後重送：同一個 key，伺服器不會多開
    expect(keys[2]).not.toBe(keys[1]); // 成功之後是新的一件事
  });

  it("切換身分清掉保存的 Idempotency-Key", () => {
    const a = stableKey("x");
    expect(stableKey("x")).toBe(a);
    clearKeys();
    expect(stableKey("x")).not.toBe(a);
  });
});

describe("排程流程（runScheduleJob）", () => {
  const run = { run_id: "plan_abc", engine: "timefold", status: "stopped", score: "0hard/-5medium/-9soft", hard: 0, medium: -5, soft: -9, initial_score: "0hard/-99medium/-99soft", score_check: true, analysis: [], operations: [], work_orders: [], kpis: { n_work_orders: 1, n_late: 0 }, axis: [], solve_ms: 1200 };
  const result = { run_id: "plan_abc", engine: "timefold", run_status: "stopped", score: run.score, hard: 0, medium: -5, soft: -9, improvements: 4, latency_ms: { build: 5, solve: 1200, total: 1300 }, egress: { images: 0, chunks: 0, bytes: 0 } };

  it("提前結束（stopped）也是成功的結果：取回 run 詳細內容，status 為 stopped", async () => {
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/schedule/jobs", () => created(SCH, "schedule"));
    t.on("GET", `/jobs/${SCH}/events`, () =>
      jobSse([["created", {}], ["started", { attempt: 1 }], ["meta", { engine: "timefold", seconds: 20 }], ["progress", { elapsed_ms: 500, score: "0hard/-9medium/-9soft", hard: 0, medium: -9, soft: -9, operations: [], kpis: {} }], ["succeeded", {}]]),
    );
    t.on("GET", `/jobs/${SCH}`, () => json(job({ id: SCH, kind: "schedule", result })));
    t.on("GET", "/schedule/runs/plan_abc", () => json(run));
    const log: string[] = [];
    await runScheduleJob(
      {},
      {
        onJob: (id) => log.push(`job:${id}`),
        onQueued: () => log.push("queued"),
        onRunning: () => log.push("running"),
        onMeta: () => log.push("meta"),
        onProgress: () => log.push("progress"),
        onSolution: (s) => log.push(`solution:${s.status}:${s.run_id}`),
        onDone: (d) => log.push(`done:${d.status}`),
        onError: (e) => log.push(`error:${e.code}`),
      },
      new AbortController().signal,
    );
    expect(log).toEqual([`job:${SCH}`, "queued", "running", "meta", "progress", "solution:stopped:plan_abc", "done:stopped"]);
  });

  it("排程範圍已有進行中的工作：接回它（重新整理後不重複建立）", async () => {
    t.on("GET", "/jobs", () => json({ items: [job({ id: SCH, kind: "schedule", status: "running" })] }));
    t.on("GET", `/jobs/${SCH}/events`, () => jobSse([["created", {}], ["cancelled", {}]]));
    t.on("GET", `/jobs/${SCH}`, () => json(job({ id: SCH, kind: "schedule", status: "cancelled" })));
    const log: string[] = [];
    await runScheduleJob({}, { onJob: (id) => log.push(id), onCancelled: () => log.push("cancelled") }, new AbortController().signal);
    expect(log).toEqual([SCH, "cancelled"]);
    expect(calls("POST", "/schedule/jobs")).toHaveLength(0);
  });

  it("失敗（輸入已變更）：回報安全的錯誤碼與訊息", async () => {
    t.on("GET", "/jobs", () => json({ items: [] }));
    t.on("POST", "/schedule/jobs", () => created(SCH, "schedule"));
    t.on("GET", `/jobs/${SCH}/events`, () => jobSse([["created", {}], ["failed", {}]]));
    t.on("GET", `/jobs/${SCH}`, () => json(job({ id: SCH, kind: "schedule", status: "failed", error: { code: "SCHEDULE_INPUT_CHANGED", message: "工單已經變更" } })));
    const errors: string[] = [];
    await runScheduleJob({}, { onError: (e) => errors.push(`${e.code}:${e.message}`) }, new AbortController().signal);
    expect(errors).toEqual(["SCHEDULE_INPUT_CHANGED:工單已經變更"]);
  });
});
