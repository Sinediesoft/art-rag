import { api, ApiError, type ChangePreviewRequest, type RouteResponse } from "../api/client";
import { runCadJob, runScheduleJob } from "../api/jobFlow";
import { streamChat, streamInventoryAsk } from "../api/sse";
import { resultUnknown } from "../api/writes";
import { withChatSettings } from "./settings";
import type { ApiFailure, ChangePart, Part, ReconstructPart, SchedulePart, Turn } from "./types";

/**
 * 執行一輪：POST /agent/route（第 1、2 段）→ 依 dispatch 呼叫真實 API／SSE。
 * 狀態透過 update 寫回 store；畫面只讀 store，所以換畫面、到功能頁再回來都不會中斷或重送。
 * 這裡不產生任何結果：每個欄位都是後端回應原封不動放進來。
 */
export type Update = (f: (t: Turn) => Turn) => void;

/** route.dispatch：交給哪個模組、帶什麼參數（後端 agent_service._dispatch） */
export interface Dispatched {
  question: string;
  part_id?: string | null;
  artwork_id?: string | null;
  part_label?: string;
  artwork_label?: string;
  path?: string;
  op?: string | null;
  compare?: { kind: "artwork" | "part"; refs: string[]; labels: string[] } | null;
  /** 跨畫作的比較題（docs/adr/039）：沒指定畫作也直接全庫問答，不改列搜尋結果 */
  global_qa?: boolean;
}

export const dispatchOf = (r: RouteResponse) => r.dispatch as unknown as Dispatched;

export function failureOf(e: unknown): ApiFailure {
  if (e instanceof ApiError) return { code: e.code, message: e.message, requestId: e.requestId, status: e.status };
  return { code: "ERROR", message: String((e as Error)?.message ?? e), requestId: "", status: 0 };
}

const setPart = <P extends Part>(update: Update, f: (p: P) => P) => update((t) => (t.part ? { ...t, part: f(t.part as P) } : t));

export interface RunOptions {
  signal: AbortSignal;
  /** 示範第 1 段：竄改過的 JWT（只在送出這一次用，不保存） */
  token?: string;
  /** /agent/route 回來的帳號是不是目前的身分；不是就不寫回任何內容，交給 onForeign 收起 */
  accept?: (accountId: string) => boolean;
  onForeign?: (route: RouteResponse) => void;
}

export async function runTurn(turn: Turn, update: Update, { signal, token, accept, onForeign }: RunOptions) {
  update((t) => ({ ...t, phase: "routing", route: null, failure: null, part: null, archived: undefined }));
  let route: RouteResponse;
  try {
    route = await api.route({ question: turn.text, image_id: turn.imageId, forced_intent: turn.forced, domain: turn.domain ?? null }, token, signal);
  } catch (e) {
    if (signal.aborted) return update((t) => ({ ...t, phase: "stopped" }));
    return update((t) => ({ ...t, phase: "error", failure: failureOf(e) }));
  }
  // 請求送出後身分換了（切換、憑證過期改發一般員工）：這是另一個身分的授權結果，不顯示、不分派
  if (accept && !accept(route.account.id)) return onForeign?.(route);
  if (signal.aborted) return update((t) => ({ ...t, phase: "stopped", route }));
  const part = initialPart(route, turn.imageId);
  update((t) => ({ ...t, route, account: { id: route.account.id, label: route.account.label }, part, phase: part.kind === "route" ? "done" : "running" }));
  if (part.kind === "route") return;
  await runPart(route, part, update, signal);
}

/** 第 1、2 段的結果決定接下來交給哪個模組 */
export function initialPart(route: RouteResponse, imageId: string | null): Part {
  const d = dispatchOf(route);
  if (route.outcome !== "pass" || route.gate === "clarify" || route.gate === "out_of_scope") return { kind: "route" };
  if (route.photo?.kind === "unknown" && !route.question) return { kind: "route" };
  if (route.gate === "modify") return { kind: "change", status: "previewing", preview: null, committed: null, approval: null, error: null };
  if (route.gate === "confirm") {
    if (route.intent === "reconstruct")
      return { kind: "reconstruct", partId: d.part_id ?? null, partLabel: d.part_label ?? null, imageId, job: null, cancelled: false };
    return { kind: "schedule", job: null, cancelled: false };
  }
  switch (route.intent) {
    case "data_query":
      return { kind: "sql", question: d.question, status: "generating", attempts: [], draft: "", result: null, answer: "", done: null, error: null };
    case "art_qa":
      if (d.artwork_id) return chatPart({ artwork_id: d.artwork_id });
      if (d.global_qa) return chatPart({});
      return { kind: "artSearch", q: d.question, status: "loading", items: [], error: null };
    case "art_search":
      return { kind: "artSearch", q: d.question, status: "loading", items: [], error: null };
    case "drawing_qa":
      if (d.part_id) return chatPart({ part_id: d.part_id });
      return { kind: "partSearch", q: d.question, status: "loading", items: [], hidden: 0, filter: null, error: null };
    case "drawing_search":
      return { kind: "partSearch", q: d.question, status: "loading", items: [], hidden: 0, filter: null, error: null };
    case "compare": {
      const c = d.compare;
      return { kind: "compare", refs: c?.refs ?? [], labels: c?.labels ?? [], status: c?.refs.length === 2 ? "loading" : "single", data: null, error: null };
    }
    default:
      return { kind: "route" };
  }
}

const chatPart = (target: { artwork_id?: string; part_id?: string }): Part => ({
  kind: "chat",
  target,
  status: "retrieving",
  sources: null,
  text: "",
  done: null,
  error: null,
});

async function runPart(route: RouteResponse, part: Part, update: Update, signal: AbortSignal) {
  const d = dispatchOf(route);
  const finish = () => update((t) => ({ ...t, phase: signal.aborted ? "stopped" : "done" }));
  try {
    switch (part.kind) {
      case "chat": {
        const fallbackQ = part.target.part_id ? "這張圖紙的重點是什麼？" : "請介紹這幅畫";
        let failed = false;
        const { body, applied } = withChatSettings({ question: d.question || fallbackQ, ...part.target, route_ticket: route.route_ticket });
        setPart<typeof part>(update, (p) => ({ ...p, applied }));
        await streamChat(
          body,
          {
            onSources: (sources) => setPart<typeof part>(update, (p) => ({ ...p, sources, status: "streaming" })),
            onToken: (text) => setPart<typeof part>(update, (p) => ({ ...p, text: p.text + text, status: "streaming" })),
            onDone: (done) => setPart<typeof part>(update, (p) => ({ ...p, done, status: "done" })),
            onError: (error) => {
              failed = true;
              setPart<typeof part>(update, (p) => ({ ...p, error, status: "error" }));
            },
          },
          signal,
        );
        if (signal.aborted) setPart<typeof part>(update, (p) => (p.status === "done" ? p : { ...p, status: "stopped" }));
        if (failed) return update((t) => ({ ...t, phase: "error" }));
        return finish();
      }
      case "sql": {
        let failed = false;
        await streamInventoryAsk(
          { question: part.question },
          {
            onAttempt: () => setPart<typeof part>(update, (p) => ({ ...p, draft: "", status: "generating" })),
            onSqlToken: (t) => setPart<typeof part>(update, (p) => ({ ...p, draft: p.draft + t })),
            onSql: (e) => setPart<typeof part>(update, (p) => ({ ...p, attempts: [...p.attempts, e], status: e.ok ? "executing" : "generating" })),
            onResult: (result) => setPart<typeof part>(update, (p) => ({ ...p, result, status: "answering" })),
            onToken: (t) => setPart<typeof part>(update, (p) => ({ ...p, answer: p.answer + t })),
            onDone: (done) => setPart<typeof part>(update, (p) => ({ ...p, done, status: "done" })),
            onError: (error) => {
              failed = true;
              setPart<typeof part>(update, (p) => ({ ...p, error, status: "error" }));
            },
          },
          signal,
        );
        if (signal.aborted) setPart<typeof part>(update, (p) => (p.status === "done" ? p : { ...p, status: "stopped" }));
        if (failed) return update((t) => ({ ...t, phase: "error" }));
        return finish();
      }
      case "artSearch": {
        const res = await api.searchText(part.q);
        if (signal.aborted) return finish();
        setPart<typeof part>(update, (p) => ({ ...p, status: "done", items: res.results.map((r) => ({ artwork: r.artwork, score: r.score })) }));
        return finish();
      }
      case "partSearch": {
        const res = await api.searchParts(part.q);
        if (signal.aborted) return finish();
        setPart<typeof part>(update, (p) => ({
          ...p,
          status: "done",
          items: res.results.map((r) => ({ part: r.part, score: r.score })),
          hidden: res.hidden ?? 0,
          filter: res.filter ?? null,
        }));
        return finish();
      }
      case "compare": {
        if (part.status === "single") return finish();
        const data = await api.compareItems(part.refs[0], part.refs[1]);
        setPart<typeof part>(update, (p) => ({ ...p, status: "done", data }));
        return finish();
      }
      case "change": {
        const req: ChangePreviewRequest = { question: route.question, op: d.op ?? null };
        const preview = await api.changePreview(req);
        setPart<ChangePart>(update, (p) => ({ ...p, status: "ready", preview }));
        return finish();
      }
      default:
        return finish();
    }
  } catch (e) {
    if (signal.aborted) return update((t) => ({ ...t, phase: "stopped" }));
    const f = failureOf(e);
    update((t) => {
      const p = t.part;
      const part2: Part | null =
        p && (p.kind === "artSearch" || p.kind === "partSearch" || p.kind === "compare")
          ? { ...p, status: "error", error: f }
          : p && p.kind === "change"
            ? { ...p, status: "error", error: `${f.message}（${f.code}）` }
            : p;
      return { ...t, part: part2, phase: "error", failure: f };
    });
  }
}

// ---------------------------------------------------------------- 使用者確認後才執行的任務

/**
 * 3D 重建：建立背景工作（POST /cad/jobs → 202）→ 訂閱事件 → 取結果。完成後模型（STL）出現在右側展示區。
 * signal 中止（離開、切換身分）只停止訂閱，工作照常在 worker 執行；要取消用 cancelTask（POST /jobs/{id}/cancel）。
 */
export async function startReconstruct(part: ReconstructPart, update: Update, signal: AbortSignal) {
  const set = (f: (p: ReconstructPart) => ReconstructPart) => setPart<ReconstructPart>(update, f);
  const job = (f: (j: NonNullable<ReconstructPart["job"]>) => NonNullable<ReconstructPart["job"]>) => set((p) => (p.job ? { ...p, job: f(p.job) } : p));
  set((p) => ({ ...p, job: { status: "queued", jobId: null, meta: null, code: "", result: null, done: null, error: null, startedAt: Date.now() } }));
  await runCadJob(
    // 和 3D 重建頁相同：附了照片就帶照片（知識庫圖紙會先依標準圖拉正），沒有就用知識庫的三視圖
    { part_id: part.partId, image_id: part.imageId, strategy: "ortho2cad" },
    {
      onJob: (jobId) => job((j) => ({ ...j, jobId })),
      onQueued: () => job((j) => ({ ...j, status: "queued" })),
      onRunning: () => job((j) => ({ ...j, status: j.status === "queued" ? "preparing" : j.status })),
      onMeta: (meta) => job((j) => ({ ...j, meta, status: "generating" })),
      onExecuting: () => job((j) => ({ ...j, status: "executing" })),
      onCode: (code) => job((j) => ({ ...j, code })),
      onResult: (result) => job((j) => ({ ...j, result })),
      onDone: (done) => job((j) => ({ ...j, done, status: "done" })),
      onError: (error) => job((j) => ({ ...j, error, status: "error" })),
      onCancelled: () => job((j) => ({ ...j, status: "cancelled" })),
    },
    signal,
  );
  if (signal.aborted) job((j) => (["done", "error", "cancelled"].includes(j.status) ? j : { ...j, status: "stopped" }));
}

/**
 * 生產排程：建立背景工作（POST /schedule/jobs → 202）→ 訂閱事件 → 取結果。結果寫回資料庫，甘特圖出現在右側展示區。
 * 離開只停止訂閱；「提前結束」（採用目前最佳解並寫回）與「取消」（不寫回）是兩個不同的操作。
 */
export async function startSchedule(update: Update, signal: AbortSignal) {
  const job = (f: (j: NonNullable<SchedulePart["job"]>) => NonNullable<SchedulePart["job"]>) =>
    setPart<SchedulePart>(update, (p) => (p.job ? { ...p, job: f(p.job) } : p));
  setPart<SchedulePart>(update, (p) => ({ ...p, job: { status: "queued", jobId: null, meta: null, progress: null, solution: null, done: null, error: null, elapsedMs: 0 } }));
  await runScheduleJob(
    {},
    {
      onJob: (jobId) => job((j) => ({ ...j, jobId })),
      onQueued: () => job((j) => ({ ...j, status: "queued" })),
      onRunning: () => job((j) => ({ ...j, status: j.status === "queued" ? "starting" : j.status })),
      onMeta: (meta) => job((j) => ({ ...j, meta, status: "solving" })),
      onProgress: (progress) => job((j) => ({ ...j, progress, elapsedMs: progress.elapsed_ms })),
      onTick: (t) => job((j) => ({ ...j, elapsedMs: t.elapsed_ms })),
      onSolution: (solution) => job((j) => ({ ...j, solution })),
      onDone: (done) => job((j) => ({ ...j, done, status: "done", elapsedMs: done.latency_ms.solve })),
      onError: (error) => job((j) => ({ ...j, error, status: "error" })),
      onCancelled: () => job((j) => ({ ...j, status: "cancelled" })),
    },
    signal,
  );
  if (signal.aborted) job((j) => (["done", "error", "cancelled"].includes(j.status) ? j : { ...j, status: "stopped" }));
}

/** 修改資料：確認寫入或送主管核准（後端再檢查一次權限與額度） */
/** 回傳伺服器的結果：done（完成）、failed（伺服器明確拒絕，確定沒寫）、unknown（結果不能確定） */
export async function commitChange(part: ChangePart, update: Update, note: string | null): Promise<"done" | "failed" | "unknown" | null> {
  if (!part.preview?.pending_id) return null;
  const id = part.preview.pending_id;
  const action = note === null ? "commit" : "approval";
  setPart<ChangePart>(update, (p) => ({ ...p, status: "committing", action, error: null }));
  try {
    if (note === null) {
      const committed = await api.changeCommit(id);
      setPart<ChangePart>(update, (p) => ({ ...p, status: "ready", committed }));
    } else {
      const approval = await api.requestApproval(id, note.trim());
      setPart<ChangePart>(update, (p) => ({ ...p, status: "ready", approval }));
    }
    return "done";
  } catch (e) {
    const f = failureOf(e);
    // 連不上、閘道逾時、5xx：請求可能已經到了伺服器，不能當成失敗讓人再按一次
    if (resultUnknown(e)) {
      setPart<ChangePart>(update, (p) => ({ ...p, status: "unconfirmed", error: `${f.message}（${f.code}）` }));
      return "unknown";
    }
    setPart<ChangePart>(update, (p) => ({ ...p, status: "ready", error: `${f.message}（${f.code}）` }));
    return "failed";
  }
}
