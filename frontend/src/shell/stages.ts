import type { RouteResponse } from "../api/client";
import type { DoneEvent, ErrorEvent, GuardCheck, JevCallInfo, PipelineStage, PostFilterInfo, SourcesEvent } from "../api/sse";
import type { Part, Turn } from "./types";

/**
 * 七段權限控管（docs/adr/015、030）的處理軌跡：
 * 認證與授權 → Jev Choice → Metadata Filter（權限感知檢索）→ Jev Noul → Jev Score → 生成閘門 → 本地 LLM。
 * 第 1、2 段來自 /agent/route；第 3～7 段來自分派到的模組（問答 SSE 的 sources／done，含後端的 pipeline 軌跡）。
 * 沒有回應的段落一律是「等待中」或「沒有執行」，前端不自己判定通過。
 */
export type StageState = "ok" | "warn" | "block" | "skip" | "doing" | "todo" | "handoff";
export type StageKey = "auth" | "guard" | "retrieve" | "verify" | "rerank" | "gate" | "gen";

export interface Stage {
  key: StageKey;
  title: string;
  short: string;
  state: StageState;
  detail: string;
  checks?: GuardCheck[];
}

/** 分派到的模組回報的進度（只有問答有第 3～7 段的即時資訊） */
export interface ModuleProgress {
  sources?: SourcesEvent | null;
  done?: DoneEvent | null;
  error?: ErrorEvent | null;
  parts?: { found: number; hidden: number; filter: string | null } | null;
  /** 分派到的模組目前的狀態（搜尋、SQL、比較、3D、排程、試算）：只有收到成功回應才算通過 */
  part?: Part | null;
  /** 這一輪已經停止或出錯：還在「執行中／等待中」的段落改成「沒有執行」 */
  halted?: boolean;
}

const kb = (b: number) => `${(b / 1024).toFixed(1)} KB`;
export const STAGE_NAMES: Record<StageKey, [string, string]> = {
  auth: ["認證與授權・JWT＋角色", "認證授權"],
  guard: ["Jev 意圖路由／防護欄・Jev Choice", "Jev Choice"],
  retrieve: ["權限感知檢索・Metadata Filter", "Metadata Filter"],
  verify: ["Jev Noul 雙重驗證・is_relevant／security_leak_check", "Jev Noul"],
  rerank: ["Jev Score 評分重排", "Jev Score"],
  gate: ["生成閘門・Generation Gate", "生成閘門"],
  gen: ["本地 LLM 生成・System 2", "本地 LLM"],
};
export const ORDER: StageKey[] = ["auth", "guard", "retrieve", "verify", "rerank", "gate", "gen"];
const GATE_TEXT: Record<RouteResponse["gate"], string> = {
  direct: "直接執行",
  confirm: "先確認",
  modify: "試算＋確認卡",
  clarify: "請你選",
  out_of_scope: "超出範圍",
};

function stage(key: StageKey, state: StageState, detail: string, extra: Partial<Stage> = {}): Stage {
  return { key, title: STAGE_NAMES[key][0], short: STAGE_NAMES[key][1], state, detail, ...extra };
}

function rest(from: StageKey, state: StageState, detail: string): Stage[] {
  return ORDER.slice(ORDER.indexOf(from)).map((k) => stage(k, state, detail));
}

/** 問答會不會走 /chat（第 3～7 段有即時資訊） */
export function isChat(route: RouteResponse) {
  const d = route.dispatch as { artwork_id?: string | null; part_id?: string | null };
  return route.outcome === "pass" && ((route.intent === "art_qa" && !!d.artwork_id) || (route.intent === "drawing_qa" && !!d.part_id));
}

function judgeText(call: JevCallInfo | null, fallback: string | null, localWhat: string) {
  if (call) return `雲端 Jev（${call.latency_ms} ms・外送 ${kb(call.bytes)} 代號化公開段落）`;
  if (fallback) return `Jev 不能用（${fallback}），${localWhat}`;
  return localWhat;
}

function verifyDetail(pf: PostFilterInfo) {
  const rr = pf.rearrange;
  const local =
    rr && rr.candidates > 1
      ? rr.fallback
        ? `相似度（Qwen3-VL 判斷失敗：${rr.fallback}）`
        : `本地 Qwen3-VL 判斷 ${rr.candidates} 段・${(rr.ms / 1000).toFixed(1)} 秒`
      : "相似度";
  const how: string[] = [];
  if (pf.verify.call || pf.verify.fallback_reason) how.push(judgeText(pf.verify.call, pf.verify.fallback_reason, "改在地端判斷"));
  if (pf.local) how.push(`${pf.local} 段${pf.mode === "jev" ? "內部／機密段落不出廠，" : ""}在地端判斷（規則掃描＋${local}）`);
  const dropped = pf.verify.checks.filter((c) => c.ok === null).length;
  return `${how.join("；") || "沒有候選段落"} → 剔除 ${pf.flagged.length} 段洩密風險${dropped > 0 ? `、${dropped} 段與提問無關` : ""}`;
}

/** 這一輪的模組進度（給第 3～7 段用） */
export function progressOf(part: Part | null, phase?: Turn["phase"]): ModuleProgress {
  const halted = phase === "stopped" || phase === "error";
  if (!part) return { halted };
  if (part.kind === "chat") return { sources: part.sources, done: part.done, error: part.error, part, halted };
  if (part.kind === "partSearch" && part.status === "done") return { parts: { found: part.items.length, hidden: part.hidden, filter: part.filter }, part, halted };
  return { part, halted };
}

type Job = { status: string; error?: { message: string; code?: string } | null } | null;
/** 任務的狀態：沒開始＝等待中、跑完＝通過、出錯＝擋下、停止＝沒有執行 */
const jobState = (j: Job, waiting: StageState = "todo"): StageState =>
  !j ? waiting : j.status === "done" ? "ok" : j.status === "error" ? "block" : j.status === "stopped" ? "skip" : "doing";

export function buildStages(route: RouteResponse, mod: ModuleProgress): Stage[] {
  const r = route;
  const d = r.dispatch as { part_label?: string; part_id?: string | null };
  const out: Stage[] = [];
  const claims = (r.auth.token?.claims ?? {}) as { name?: string; roles?: string[]; clearance?: number };

  // ---- 第 1 段：認證（閘道已驗 JWT）＋授權（角色能不能做這件事）
  const a = r.auth;
  const token = `JWT ${r.auth.token?.alg ?? ""} 簽章、效期都通過（${claims.name ?? r.account.label}・roles=${JSON.stringify(claims.roles ?? [r.account.role])}・clearance=${claims.clearance ?? r.account.clearance}）`;
  out.push(
    stage(
      "auth",
      !a.passed ? "block" : a.pending || a.checks.some((c) => c.warn) ? "warn" : "ok",
      `${token} → ${
        a.pending ? "要做什麼還不確定，等你選擇後再檢查功能與動作權限" : a.passed ? "角色可以做這件事" : a.degraded ? "降級回應：查無資料（不透露有沒有這份文件）" : `未通過：${a.reason}`
      }`,
      { checks: a.checks },
    ),
  );

  // ---- 第 2 段：本地分流（交給哪個模組）＋ Jev Choice（正常查詢／注入／閒聊）
  const photo = r.photo
    ? `照片在本機辨識（Chinese-CLIP）：${r.photo.kind === "art" ? "畫作" : r.photo.kind === "drawing" ? "工廠圖紙" : "比對不到"}${r.photo.id ? `〈${r.photo.label}〉` : ""}；`
    : "";
  const routed =
    r.router.engine === "user" ? `你選了〈${r.intent_label}〉` : r.gate === "clarify" ? `本地分流：${r.gate_reason}` : `本地分流（${r.router.model}）：${r.intent_label} ${r.confidence.toFixed(2)}`;
  const pii = r.pii.length ? `；已遮蔽個資 ${r.pii.length} 筆` : "";
  const g = r.guard;
  if (!g) out.push(stage("guard", "skip", "第 1 段沒過，沒有送給 Jev"));
  else if (g.engine === "skip") out.push(stage("guard", "skip", `${photo}${routed}；${g.skipped ?? "沒有文字要判斷"}`));
  else {
    const judge = g.call
      ? `Jev Choice（${g.call.model}・${g.call.latency_ms} ms・外送 ${kb(g.call.bytes)} 代號化文字）`
      : `地端規則（${g.fallback_reason ? `Jev 不能用：${g.fallback_reason}；` : ""}外送 0 B）`;
    const verdict = g.verdict === "attack" ? `攔截：${g.reason}` : g.verdict === "chitchat" ? "無關閒聊 → 快速短路回覆，不檢索、不生成" : "正常查詢 → 進入檢索";
    out.push(
      stage("guard", !g.passed ? "block" : g.verdict === "chitchat" || g.checks.some((c) => c.warn) || g.fallback_reason ? "warn" : "ok", `${photo}${routed}${pii}；${judge} → ${verdict}`, {
        checks: g.checks,
      }),
    );
  }

  // ---- 第 3～7 段
  if (r.outcome === "short_circuit") {
    out.push(...rest("retrieve", "skip", "快速短路，不檢索"));
    out[out.length - 1] = stage("gen", "ok", "固定回覆系統能做的事（沒有呼叫 LLM）", { title: "快速短路回覆", short: "短路回覆" });
    return out;
  }
  if (r.outcome !== "pass") {
    out.push(...rest("retrieve", "skip", r.outcome === "blocked_guard" ? "Jev Choice 擋下，沒有檢索" : "沒有執行"));
    out[out.length - 1] = stage("gen", "skip", "沒有碰到任何資料與模型");
    return out;
  }
  if (r.gate === "clarify") {
    out.push(...rest("retrieve", "skip", "等你選擇"));
    return out;
  }
  const reply = (detail: string) => stage("gen", "ok", detail, { title: "直接回覆", short: "回覆" });
  const handoff = (detail: string, title: string, short: string) => stage("gen", "handoff", detail, { title, short });
  const filterText = r.auth.filter?.text;
  const none = (msg: string) => [stage("verify", "skip", msg), stage("rerank", "skip", msg), stage("gate", "skip", msg)];

  if (r.photo?.kind === "unknown" && !r.question) {
    const guess = r.photo.domain === "art" ? "本機推測風格、題材、媒材（沒有出處）；" : "";
    out.push(stage("retrieve", "skip", "照片比對不到知識庫，沒有檢索"), ...none("沒有段落"), reply(`${guess}請你補一句說明`));
  } else if (r.intent === "out_of_scope") {
    out.push(stage("retrieve", "skip", "不需檢索"), ...none("沒有段落"), reply("固定回覆系統能做的事"));
  } else if (r.intent === "system") {
    out.push(stage("retrieve", "skip", "不需檢索"), ...none("沒有段落"), reply("讀取記憶體、服務狀態與攔截紀錄"));
  } else if (r.intent === "batch_identify") {
    out.push(
      stage("retrieve", "skip", "照片在功能頁上一次選一批，不在對話裡檢索"),
      ...none("沒有段落"),
      handoff("交給批次辨識：每張照片用 Chinese-CLIP＋幾何驗證，照你的資料範圍顯示", "批次辨識", "批次辨識"),
    );
  } else if (r.intent === "compare") {
    // 讀取兩件：/compare/items 回來才算通過
    const p = mod.part?.kind === "compare" ? mod.part : null;
    const st: StageState = !p ? "todo" : p.status === "single" ? "skip" : p.status === "loading" ? "doing" : p.status === "error" ? "block" : "ok";
    const detail =
      st === "skip"
        ? "只提到一件，等你在比較頁選另一件"
        : st === "block"
          ? `讀取失敗：${p!.error?.message}（${p!.error?.code}）`
          : st === "ok"
            ? `讀取兩件的知識庫資料・${filterText ?? ""} → ${p!.data?.differences ?? 0} 個欄位不同`
            : `讀取兩件的知識庫資料・${filterText ?? ""}`;
    out.push(
      stage("retrieve", st, detail, { title: "權限感知檢索・讀取兩件", short: "讀資料" }),
      ...none("比較表直接讀知識庫欄位，不放進生成上下文"),
      stage("gen", st === "ok" ? "skip" : st === "doing" ? "todo" : "skip", "並排比較表不經生成；差異摘要要在比較頁按了才請本地模型寫", { title: "兩件並排比較", short: "並排比較" }),
    );
  } else if (isChat(r)) {
    out.push(...chatStages(r, mod, filterText));
  } else if (r.intent === "art_search" || r.intent === "art_qa") {
    // 以文搜畫：/search/text 回來才算通過；失敗就是擋下（403、連不上…）
    const p = mod.part?.kind === "artSearch" ? mod.part : null;
    const st: StageState = !p || p.status === "loading" ? "doing" : p.status === "error" ? "block" : "ok";
    out.push(
      stage(
        "retrieve",
        st,
        st === "ok"
          ? `Chinese-CLIP＋bge-m3 以文搜畫・${filterText ?? ""} → ${p!.items.length} 幅`
          : st === "block"
            ? `以文搜畫失敗：${p!.error?.message}（${p!.error?.code}）`
            : `Chinese-CLIP＋bge-m3 以文搜畫・${filterText ?? ""}`,
      ),
      ...none("搜尋結果直接列出畫作，不放進生成上下文"),
      stage("gen", st === "doing" ? "todo" : "skip", "直接列出最符合的畫作，不經生成"),
    );
  } else if (r.intent === "drawing_search" || r.intent === "drawing_qa") {
    const p = mod.part?.kind === "partSearch" ? mod.part : null;
    const found = mod.parts;
    const st: StageState = p?.status === "error" ? "block" : found ? "ok" : "doing";
    out.push(
      st === "ok"
        ? stage("retrieve", "ok", `bge-m3 檢索製程文件・${found!.filter ?? filterText ?? ""} → ${found!.found} 張${found!.hidden ? `（另有 ${found!.hidden} 張不在你的資料範圍，檢索時就被濾掉）` : ""}`)
        : st === "block"
          ? stage("retrieve", "block", `圖紙查找失敗：${p!.error?.message}（${p!.error?.code}）`)
          : stage("retrieve", "doing", `bge-m3 檢索製程文件・${filterText ?? ""}`),
      ...none("搜尋結果直接列出圖紙，不放進生成上下文"),
      stage("gen", st === "doing" ? "todo" : "skip", "直接列出圖紙，不經生成"),
    );
  } else if (r.intent === "data_query") {
    // Text-to-SQL：有結果表才算查詢通過；回答串流完才算生成通過
    const p = mod.part?.kind === "sql" ? mod.part : null;
    const q: StageState = !p ? "todo" : p.result ? "ok" : p.status === "error" ? "block" : p.status === "stopped" ? "skip" : "doing";
    const g: StageState = !p || !p.result ? (q === "block" || q === "skip" ? "skip" : "todo") : p.status === "done" ? "ok" : p.status === "error" ? "block" : p.status === "stopped" ? "skip" : "doing";
    out.push(
      stage(
        "retrieve",
        q,
        q === "ok"
          ? `地端 LLM 產生 SQL → 唯讀檢查 → 執行 → ${p!.result!.row_count} 筆（資料庫查詢，不經向量檢索）`
          : q === "block"
            ? `查詢失敗：${p!.error?.message}（${p!.error?.code}）`
            : "地端 LLM 產生 SQL → 唯讀檢查 → 執行（資料庫查詢，不經向量檢索）",
        { title: "權限感知檢索・唯讀資料庫查詢", short: "查詢" },
      ),
      ...none("查詢結果是資料庫數字，由唯讀 SQL 檢查把關，不經文件驗證"),
      stage("gen", g, g === "ok" ? `地端 LLM（${p!.done?.model ?? ""}）依查詢結果回答` : g === "block" ? `回答失敗：${p!.error?.message}` : "地端 LLM 依查詢結果回答"),
    );
  } else if (r.intent === "reconstruct") {
    // 3D 重建：按「開始轉換」前是等待中；讀到圖紙（meta）才算讀取通過，Ortho2CAD 跑完才算完成
    const p = mod.part?.kind === "reconstruct" ? mod.part : null;
    const j = p?.job ?? null;
    const waiting: StageState = p?.cancelled ? "skip" : "todo";
    const read: StageState = !j ? waiting : j.meta ? "ok" : jobState(j);
    const gen: StageState = !j ? waiting : j.status === "done" && j.result && !j.result.ok ? "block" : jobState(j);
    out.push(
      d.part_id || p?.imageId
        ? stage("retrieve", read, read === "ok" ? `讀取${d.part_label ? `〈${d.part_label}〉的` : "附上的"}圖紙` : read === "skip" ? "已取消" : read === "block" ? `讀取失敗：${j?.error?.message}` : "等你按「開始轉換」", {
            title: "權限感知檢索・讀取圖紙",
            short: "讀圖紙",
          })
        : stage("retrieve", "skip", "等你選圖紙", { title: "權限感知檢索・讀取圖紙", short: "讀圖紙" }),
      ...none("圖紙影像直接交給 Ortho2CAD，不經文字驗證"),
      stage(
        "gen",
        gen,
        gen === "ok" ? `Ortho2CAD（${j?.done?.model ?? ""}）產生模型並通過驗證` : gen === "block" ? `3D 重建失敗：${j?.error?.message ?? j?.result?.error ?? ""}` : gen === "todo" ? "等你確認後交給 Ortho2CAD（地端）" : gen === "skip" ? "沒有執行" : "Ortho2CAD 產生中",
        { title: "Ortho2CAD 3D 重建", short: "3D 重建" },
      ),
    );
  } else if (r.intent === "schedule") {
    const p = mod.part?.kind === "schedule" ? mod.part : null;
    const st = jobState(p?.job ?? null, p?.cancelled ? "skip" : "todo");
    out.push(
      stage("retrieve", "skip", "不需檢索"),
      ...none("沒有段落"),
      stage(
        "gen",
        st,
        st === "ok" ? `${p?.job?.meta?.engine_label ?? "排程"}求解完成，結果已寫回` : st === "block" ? `排程失敗：${p?.job?.error?.message}` : st === "todo" ? "等你確認後交給 Timefold（地端）" : st === "skip" ? "沒有執行" : "求解中",
        { title: "Timefold 排程", short: "排程" },
      ),
    );
  } else if (r.intent === "modify") {
    const p = mod.part?.kind === "change" ? mod.part : null;
    const st: StageState = !p || p.status === "previewing" ? "doing" : !p.preview ? "block" : p.preview.next === "rejected" ? "block" : "ok";
    out.push(
      stage("retrieve", "skip", "不需檢索（試算時讀取目前資料）"),
      ...none("沒有段落；修改資料另有範圍／欄位／上限／額度檢查"),
      stage(
        "gen",
        st,
        st === "ok" ? `試算完成：${p!.preview!.message}` : st === "block" ? (p?.preview ? `已拒絕：${p.preview.message}` : `試算失敗：${p?.error ?? ""}`) : "參數抽取 → 範圍／欄位／上限 → 副本試算 → 額度判斷",
        { title: "修改資料流程", short: "試算" },
      ),
    );
  } else {
    out.push(...rest("retrieve", "skip", "沒有執行"));
  }
  return halt(out, mod);
}

/** 這一輪已經停止或出錯：還沒有結果的段落一律是「沒有執行」，不會一直轉圈或被當成通過 */
function halt(stages: Stage[], mod: ModuleProgress): Stage[] {
  if (!mod.halted) return stages;
  return stages.map((s) => (s.state === "doing" || s.state === "todo" ? { ...s, state: "skip", detail: `沒有完成（已停止或出錯）・${s.detail}` } : s));
}

function chatStages(r: RouteResponse, mod: ModuleProgress, filterText: string | undefined): Stage[] {
  const out: Stage[] = [];
  const { sources, done, error } = mod;
  const pf = sources?.post_filter;
  const mfg = r.intent === "drawing_qa";
  if (error && !sources) return [stage("retrieve", "block", error.message), ...rest("verify", "skip", "沒有執行")];
  if (!sources) return [stage("retrieve", "doing", `bge-m3 向量檢索・${filterText ?? ""}`), ...rest("verify", "todo", "等檢索結果")];
  const n = sources.candidates ?? sources.sources.length;
  out.push(stage("retrieve", n ? "ok" : "warn", `bge-m3 向量檢索・${sources.filter?.text ?? filterText} → ${n ? `${n} 段候選` : "0 段（權限內沒有符合的文件塊）"}`));
  if (!pf) out.push(...rest("verify", "skip", "沒有段落要驗證").slice(0, 3));
  else {
    out.push(stage("verify", pf.flagged.length ? "warn" : pf.candidates ? "ok" : "skip", verifyDetail(pf), { checks: pf.verify.checks }));
    const rr = pf.rerank;
    out.push(
      rr
        ? stage(
            "rerank",
            rr.checks.length ? "ok" : "skip",
            rr.checks.length ? `${judgeText(rr.call, rr.fallback_reason, "在地端依相似度排序")} → 留 ${rr.checks.filter((c) => c.ok).length} 段（最多 3 段）` : "沒有段落通過第 4 段",
            { checks: rr.checks },
          )
        : stage("rerank", "skip", "沒有重排"),
    );
    const gt = pf.gate;
    out.push(
      gt
        ? stage(
            "gate",
            gt.passed ? (gt.checks.some((c) => c.warn) ? "warn" : "ok") : "block",
            `${judgeText(gt.call, gt.fallback_reason, mfg ? "機密段落不送 Jev，在地端判斷" : "在地端判斷")} → ${gt.passed ? "可答且合規，交給本地 LLM" : "降級回應：查無資料（不呼叫 LLM）"}`,
            { checks: gt.checks },
          )
        : stage("gate", "skip", "沒有生成閘門"),
    );
  }
  out.push(
    done?.degraded
      ? stage("gen", "skip", "生成閘門沒過，沒有呼叫 LLM（回覆「查無資料」）")
      : done
        ? stage(
            "gen",
            "ok",
            `本地 LLM（${done.model}）只依 ${sources.sources.length} 段乾淨上下文生成${mfg ? "（機密圖紙全程地端）" : ""}・首字 ${((done.latency_ms.first_token ?? 0) / 1000).toFixed(1)} 秒`,
          )
        : error
          ? stage("gen", "block", error.message)
          : stage("gen", "doing", `本地 LLM 只依第 5 段留下的段落生成${mfg ? "（機密圖紙全程地端）" : ""}`),
  );
  return overlayPipeline(out, done?.pipeline ?? sources.pipeline ?? null, !!done);
}

/**
 * 後端的可觀測軌跡（docs/adr/030）：依序列出實際執行過的段落與結果，擋下之後的段落不列。
 * 有軌跡時，第 3～7 段的通過／擋下／略過一律照它；軌跡沒列到的段落＝沒有執行（complete 時）或還在等（串流中）。
 */
export function overlayPipeline(stages: Stage[], pipeline: PipelineStage[] | null, complete: boolean): Stage[] {
  if (!pipeline?.length) return stages;
  const byNo = new Map(pipeline.map((p) => [p.stage, p]));
  return stages.map((s) => {
    const no = ORDER.indexOf(s.key) + 1;
    if (no <= 2) return s; // 第 1、2 段以 /agent/route 的判斷為準
    const p = byNo.get(no as PipelineStage["stage"]);
    if (!p) return complete ? { ...s, state: "skip", detail: `沒有執行（後端軌跡未列）・${s.detail}` } : s;
    const state: StageState = p.status === "block" ? "block" : p.status === "skip" ? "skip" : s.state === "warn" ? "warn" : "ok";
    return { ...s, state, detail: `${s.detail}${p.by ? `（${p.by}）` : ""}${p.detail && !s.detail.includes(p.detail) ? `・${p.detail}` : ""}` };
  });
}

/** 第 1 段在 API 閘道就被擋（401）：請求沒有進到路由，後面六段都沒有執行 */
export function gatewayStages(code: string, message: string): Stage[] {
  return [stage("auth", "block", `API 閘道拒絕連線（401 ${code}）：${message}`), ...rest("guard", "skip", "請求被閘道擋下，沒有碰到任何模型與資料")];
}

export function summaryOf(route: RouteResponse, mod: ModuleProgress) {
  const b = route.blocked;
  if (b?.degraded) return "降級回應：查無資料";
  if (b) return `${b.stage === 1 ? "認證與授權" : "Jev Choice"}擋下：${b.rule}・已記錄 ${b.log_no}`;
  if (route.outcome === "short_circuit") return "無關閒聊 → 快速短路回覆";
  if (route.gate === "clarify") return "要做什麼不確定 → 請你選";
  const pf = mod.sources?.post_filter;
  if (pf?.gate && !pf.gate.passed) return `${route.intent_label}・生成閘門：查無資料`;
  if (mod.done?.degraded) return `${route.intent_label}・生成閘門：查無資料`;
  const removed = pf?.flagged.length;
  return `${route.intent_label}・${removed ? `剔除 ${removed} 段洩密風險・` : ""}${GATE_TEXT[route.gate]}`;
}

/** 這一輪送出本機（給雲端 Jev）的位元組數：第 2 段＋第 4～6 段 */
export function egressOf(route: RouteResponse, mod: ModuleProgress) {
  const pf = mod.sources?.post_filter;
  const calls = [route.guard?.call, pf?.verify.call, pf?.rerank?.call, pf?.gate?.call].filter(Boolean) as JevCallInfo[];
  return { bytes: calls.reduce((n, c) => n + c.bytes, 0), calls };
}
