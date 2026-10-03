import { useState } from "react";
import type { ApiError, RouteResponse } from "../../api/client";
import type { DoneEvent, ErrorEvent, GuardCheck, JevCallInfo, PostFilterInfo, SourcesEvent } from "../../api/sse";

/**
 * 七段權限控管的處理過程（docs/adr/015）：收合時是一行流程列
 * 「認證授權 › Jev Choice › 檢索 › Jev Noul › Jev Score › 生成閘門 › 本地 LLM」＋摘要，
 * 點開看每一段的檢查、JWT、Metadata Filter、每個候選段落的去留與分數、意圖機率、送給 Jev 的代號化內容與請求本文。
 * 第 1、2 段來自 /agent/route；第 3～7 段來自分派到的模組（問答的 sources／done 事件）。
 */

export type StageState = "ok" | "warn" | "block" | "skip" | "doing" | "todo" | "handoff";

export interface Stage {
  key: "auth" | "guard" | "retrieve" | "verify" | "rerank" | "gate" | "gen";
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
  /** 圖紙查找：不在資料範圍、檢索時就濾掉的張數與 filter */
  parts?: { found: number; hidden: number; filter: string | null } | null;
}

const kb = (b: number) => `${(b / 1024).toFixed(1)} KB`;
const NAMES: Record<Stage["key"], [string, string]> = {
  auth: ["認證與授權・JWT＋角色", "認證授權"],
  guard: ["Jev 意圖路由／防護欄・Jev Choice", "Jev Choice"],
  retrieve: ["權限感知檢索・Metadata Filter", "檢索"],
  verify: ["Jev Noul 雙重驗證・is_relevant／security_leak_check", "Jev Noul"],
  rerank: ["Jev Score 評分重排", "Jev Score"],
  gate: ["生成閘門・Generation Gate", "生成閘門"],
  gen: ["本地 LLM 生成・System 2", "本地 LLM"],
};
const ORDER: Stage["key"][] = ["auth", "guard", "retrieve", "verify", "rerank", "gate", "gen"];
const GATE_TEXT: Record<RouteResponse["gate"], string> = {
  direct: "直接執行",
  confirm: "先確認",
  modify: "試算＋確認卡",
  clarify: "請你選",
  out_of_scope: "超出範圍",
};
const VERDICT: Record<string, string> = { query: "正常查詢", attack: "Prompt 注入", chitchat: "無關閒聊" };

function stage(key: Stage["key"], state: StageState, detail: string, extra: Partial<Stage> = {}): Stage {
  return { key, title: NAMES[key][0], short: NAMES[key][1], state, detail, ...extra };
}

/** 後面幾段都一樣的狀態（沒執行、等你選…） */
function rest(from: Stage["key"], state: StageState, detail: string): Stage[] {
  return ORDER.slice(ORDER.indexOf(from)).map((k) => stage(k, state, detail));
}

/** 問答會不會走 /chat（第 3～7 段有即時資訊） */
export function isChat(route: RouteResponse) {
  const d = route.dispatch as { artwork_id?: string | null; part_id?: string | null };
  return (route.intent === "art_qa" && !!d.artwork_id) || (route.intent === "drawing_qa" && !!d.part_id);
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
  if (pf.verify.call || pf.verify.fallback_reason)
    how.push(judgeText(pf.verify.call, pf.verify.fallback_reason, "改在地端判斷"));
  if (pf.local) how.push(`${pf.local} 段${pf.mode === "jev" ? "內部／機密段落不出廠，" : ""}在地端判斷（規則掃描＋${local}）`);
  const dropped = pf.verify.checks.filter((c) => c.ok === null).length;
  return `${how.join("；") || "沒有候選段落"} → 剔除 ${pf.flagged.length} 段洩密風險${dropped > 0 ? `、${dropped} 段與提問無關` : ""}`;
}

export function buildStages(route: RouteResponse, mod: ModuleProgress): Stage[] {
  const r = route;
  const d = r.dispatch as { part_label?: string; artwork_label?: string; part_id?: string | null };
  const out: Stage[] = [];
  const claims = r.auth.token.claims as { name?: string; roles?: string[]; dept?: string; clearance?: number };

  // ---- 第 1 段：認證（閘道已驗 JWT）＋授權（角色能不能做這件事）
  const a = r.auth;
  const token = `JWT ${r.auth.token.alg} 簽章、效期都通過（${claims.name}・roles=${JSON.stringify(claims.roles)}・clearance=${claims.clearance}）`;
  out.push(
    stage(
      "auth",
      !a.passed ? "block" : a.pending || a.checks.some((c) => c.warn) ? "warn" : "ok",
      `${token} → ${
        a.pending
          ? "要做什麼還不確定，等你選擇後再檢查功能與動作權限"
          : a.passed
            ? "角色可以做這件事"
            : a.degraded
              ? "🔒 降級回應：查無資料（不透露有沒有這份文件）"
              : `未通過：${a.reason}`
      }`,
      { checks: a.checks },
    ),
  );

  // ---- 第 2 段：本地分流（交給哪個模組）＋ Jev Choice（正常查詢／注入／閒聊）
  const photo = r.photo
    ? `照片在本機辨識（Chinese-CLIP）：${r.photo.kind === "art" ? "畫作" : r.photo.kind === "drawing" ? "工廠圖紙" : "比對不到"}${
        r.photo.id ? `〈${r.photo.label}〉` : ""
      }；`
    : "";
  const routed =
    r.router.engine === "user"
      ? `你選了〈${r.intent_label}〉`
      : r.gate === "clarify"
        ? `本地分流：${r.gate_reason}`
        : `本地分流（${r.router.model}）：${r.intent_label} ${r.confidence.toFixed(2)}`;
  const pii = r.pii.length ? `；已遮蔽個資 ${r.pii.length} 筆` : "";
  const g = r.guard;
  if (!g) out.push(stage("guard", "skip", "第 1 段沒過，沒有送給 Jev"));
  else if (g.engine === "skip") out.push(stage("guard", "skip", `${photo}${routed}；${g.skipped ?? "沒有文字要判斷"}`));
  else {
    const judge = g.call
      ? `Jev Choice（${g.call.model}・${g.call.latency_ms} ms・外送 ${kb(g.call.bytes)} 代號化文字）`
      : `地端規則（${g.fallback_reason ? `Jev 不能用：${g.fallback_reason}；` : ""}外送 0 B）`;
    const verdict =
      g.verdict === "attack"
        ? `攔截：${g.reason}`
        : g.verdict === "chitchat"
          ? "無關閒聊 → 💬 快速短路回覆，不檢索、不生成"
          : "正常查詢 → 進入檢索";
    out.push(
      stage(
        "guard",
        !g.passed ? "block" : g.verdict === "chitchat" || g.checks.some((c) => c.warn) || g.fallback_reason ? "warn" : "ok",
        `${photo}${routed}${pii}；${judge} → ${verdict}`,
        { checks: g.checks },
      ),
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
    out.push(stage("retrieve", "skip", "照片比對不到知識庫，沒有檢索"), ...none("沒有段落"), reply("請你補一句說明"));
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
    out.push(
      stage("retrieve", "ok", `讀取兩件的知識庫資料・${filterText ?? ""}`, { title: "權限感知檢索・讀取兩件", short: "讀資料" }),
      ...none("比較表直接讀知識庫欄位，不放進生成上下文"),
      handoff("並排比較表（不經生成）；差異摘要要在比較頁按了才請本地模型寫", "兩件並排比較", "並排比較"),
    );
  } else if (isChat(r)) {
    const { sources, done, error } = mod;
    const pf = sources?.post_filter;
    const mfg = r.intent === "drawing_qa";
    if (error && !sources) {
      out.push(stage("retrieve", "block", error.message), ...rest("verify", "skip", "沒有執行"));
    } else if (!sources) {
      out.push(stage("retrieve", "doing", `bge-m3 向量檢索・${filterText ?? ""}`), ...rest("verify", "todo", "等檢索結果"));
    } else {
      const n = sources.candidates ?? sources.sources.length;
      out.push(
        stage(
          "retrieve",
          n ? "ok" : "warn",
          `bge-m3 向量檢索・${sources.filter?.text ?? filterText} → ${n ? `${n} 段候選` : "0 段（權限內沒有符合的文件塊）"}`,
        ),
      );
      if (!pf) out.push(...rest("verify", "skip", "沒有段落要驗證"));
      else {
        out.push(
          stage("verify", pf.flagged.length ? "warn" : pf.candidates ? "ok" : "skip", verifyDetail(pf), {
            checks: pf.verify.checks,
          }),
        );
        const rr = pf.rerank;
        out.push(
          rr
            ? stage(
                "rerank",
                rr.checks.length ? "ok" : "skip",
                rr.checks.length
                  ? `${judgeText(rr.call, rr.fallback_reason, "在地端依相似度排序")} → 留 ${rr.checks.filter((c) => c.ok).length} 段（最多 3 段）`
                  : "沒有段落通過第 4 段",
                { checks: rr.checks },
              )
            : stage("rerank", "skip", "其他頁面不重排"),
        );
        const gt = pf.gate;
        out.push(
          gt
            ? stage(
                "gate",
                gt.passed ? (gt.checks.some((c) => c.warn) ? "warn" : "ok") : "block",
                `${judgeText(gt.call, gt.fallback_reason, mfg ? "機密段落不送 Jev，在地端判斷" : "在地端判斷")} → ${
                  gt.passed ? "可答且合規，交給本地 LLM" : "🔒 降級回應：查無資料（不呼叫 LLM）"
                }`,
                { checks: gt.checks },
              )
            : stage("gate", "skip", "其他頁面沒有生成閘門"),
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
    }
  } else if (r.intent === "art_search" || r.intent === "art_qa") {
    out.push(
      stage("retrieve", "ok", `Chinese-CLIP＋bge-m3 以文搜畫・${filterText ?? ""}`),
      ...none("搜尋結果直接列出畫作，不放進生成上下文"),
      stage("gen", "skip", "直接列出最符合的畫作，不經生成"),
    );
  } else if (r.intent === "drawing_search" || r.intent === "drawing_qa") {
    const p = mod.parts;
    out.push(
      p
        ? stage(
            "retrieve",
            "ok",
            `bge-m3 檢索製程文件・${p.filter ?? filterText ?? ""} → ${p.found} 張${p.hidden ? `（另有 ${p.hidden} 張不在你的資料範圍，檢索時就被濾掉）` : ""}`,
          )
        : stage("retrieve", "doing", `bge-m3 檢索製程文件・${filterText ?? ""}`),
      ...none("搜尋結果直接列出圖紙，不放進生成上下文"),
      stage("gen", "skip", "直接列出圖紙，不經生成"),
    );
  } else if (r.intent === "data_query") {
    out.push(
      stage("retrieve", "handoff", "地端 LLM 產生 SQL → 唯讀檢查 → SQLite 執行（資料庫查詢，不經向量檢索）", {
        title: "權限感知檢索・唯讀資料庫查詢",
        short: "查詢",
      }),
      ...none("查詢結果是資料庫數字，由唯讀 SQL 檢查把關，不經文件驗證"),
      handoff("地端 LLM 依查詢結果回答（進度見下方）", "本地 LLM 生成・System 2", "本地 LLM"),
    );
  } else if (r.intent === "reconstruct") {
    out.push(
      d.part_id
        ? stage("retrieve", "ok", `讀取〈${d.part_label}〉的圖紙（第 1 段已允許）`, { title: "權限感知檢索・讀取圖紙", short: "讀圖紙" })
        : stage("retrieve", "skip", "等你選圖紙", { title: "權限感知檢索・讀取圖紙", short: "讀圖紙" }),
      ...none("圖紙影像直接交給 Ortho2CAD，不經文字驗證"),
      handoff("等你確認後交給 Ortho2CAD（地端）", "Ortho2CAD 3D 重建", "3D 重建"),
    );
  } else if (r.intent === "schedule") {
    out.push(stage("retrieve", "skip", "不需檢索"), ...none("沒有段落"), handoff("等你確認後交給 Timefold（地端）", "Timefold 排程", "排程"));
  } else if (r.intent === "modify") {
    out.push(
      stage("retrieve", "skip", "不需檢索（試算時讀取目前資料）"),
      ...none("沒有段落；修改資料另有範圍／欄位／上限／額度檢查"),
      handoff("參數抽取 → 範圍／欄位／上限 → 副本試算 → 額度判斷", "修改資料流程", "試算"),
    );
  }
  return out;
}

function summaryOf(route: RouteResponse, mod: ModuleProgress) {
  const b = route.blocked;
  if (b?.degraded) return "🔒 降級回應：查無資料";
  if (b) return `${b.stage === 1 ? "認證與授權" : "Jev Choice"}擋下：${b.rule}・已記錄 ${b.log_no}`;
  if (route.outcome === "short_circuit") return "💬 無關閒聊 → 快速短路回覆";
  if (route.gate === "clarify") return "要做什麼不確定 → 請你選";
  const pf = mod.sources?.post_filter;
  if (pf?.gate && !pf.gate.passed) return `${route.intent_label}・🔒 生成閘門：查無資料`;
  const removed = pf?.flagged.length;
  return `${route.intent_label}・${removed ? `剔除 ${removed} 段洩密風險・` : ""}${GATE_TEXT[route.gate]}`;
}

const DOT: Record<StageState, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  block: "bg-danger",
  handoff: "bg-accent",
  skip: "",
  todo: "",
  doing: "",
};

function Dot({ state, size = "h-2 w-2" }: { state: StageState; size?: string }) {
  if (state === "doing")
    return <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-hairline border-t-accent" />;
  if (state === "skip" || state === "todo")
    return <span className={`${size} inline-block shrink-0 rounded-full border border-ink-48`} />;
  return <span className={`${size} inline-block shrink-0 rounded-full ${DOT[state]}`} />;
}

function FlowBar({ stages }: { stages: Stage[] }) {
  return (
    <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
      <span className="mr-1 font-semibold text-ink-48">處理過程</span>
      {stages.map((s, i) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          {i > 0 && <span className="text-ink-48/60">›</span>}
          <Dot state={s.state} />
          <span
            className={
              s.state === "block"
                ? "font-semibold text-danger"
                : s.state === "skip" || s.state === "todo"
                  ? "text-ink-48"
                  : s.state === "doing"
                    ? "font-semibold text-accent"
                    : "font-normal text-ink"
            }
          >
            {s.short}
          </span>
        </span>
      ))}
    </span>
  );
}

function StageList({ stages }: { stages: Stage[] }) {
  return (
    <ol className="flex flex-col gap-3">
      {stages.map((s, i) => (
        <li key={s.key} className="flex gap-2.5">
          <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-card font-mono text-[11px] font-semibold text-ink-80 ring-1 ring-hairline">
            {i + 1}
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-1.5">
              <Dot state={s.state} />
              <span
                className={`font-semibold ${s.state === "block" ? "text-danger" : s.state === "skip" || s.state === "todo" ? "text-ink-48" : s.state === "doing" ? "text-accent" : "text-ink"}`}
              >
                {s.title}
              </span>
            </span>
            <span className="mt-0.5 block text-ink-80">{s.detail}</span>
            {s.checks && s.checks.length > 0 && <Checks checks={s.checks} />}
          </span>
        </li>
      ))}
    </ol>
  );
}

export function PipelineTrace({ route, mod }: { route: RouteResponse; mod: ModuleProgress }) {
  const [open, setOpen] = useState(false);
  const stages = buildStages(route, mod);
  const pf = mod.sources?.post_filter;
  const calls = [route.guard?.call, pf?.verify.call, pf?.rerank?.call, pf?.gate?.call].filter(Boolean) as JevCallInfo[];
  const egress = calls.reduce((n, c) => n + c.bytes, 0);
  const blocked = !!route.blocked;

  return (
    <div className="rounded-lg border border-hairline bg-parchment text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full flex-col items-start gap-1.5 p-3 text-left"
      >
        <span className="flex flex-wrap items-center gap-x-1.5">
          <FlowBar stages={stages} />
          <span className={`ml-1 text-ink-48 transition ${open ? "rotate-90" : ""}`}>›</span>
        </span>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={blocked ? "font-semibold text-danger" : "text-ink-80"}>{summaryOf(route, mod)}</span>
          <span
            className={`rounded-full px-2 py-0.5 font-mono font-semibold ${egress ? "bg-warning-soft text-warning" : "bg-success-soft text-success"}`}
            title={egress ? "只送遮蔽個資、代號化後的文字與公開段落；照片、原始名稱、機密段落都不送" : "全程在本機"}
          >
            {egress ? `外送 ${kb(egress)} → Jev${calls.length > 1 ? `（${calls.length} 次）` : ""}` : "外送 0 B・全程地端"}
          </span>
          <span className="text-ink-48">點開看每一段的檢查</span>
        </span>
      </button>

      {open && (
        <div className="border-t border-hairline px-3 pb-3 pt-3">
          <StageList stages={stages} />
          <Details route={route} calls={calls} />
        </div>
      )}
    </div>
  );
}

/** 第 1 段在 API 閘道就被擋（401）：請求沒有進到路由，後面六段都沒有執行 */
export function GatewayDenied({ error }: { error: ApiError }) {
  const stages: Stage[] = [
    stage("auth", "block", `API 閘道拒絕連線（401 ${error.code}）：${error.message}`),
    ...rest("guard", "skip", "請求被閘道擋下，沒有碰到任何模型與資料"),
  ];
  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-lg border border-hairline bg-parchment p-3 text-xs">
        <FlowBar stages={stages} />
        <div className="mt-3 border-t border-hairline pt-3">
          <StageList stages={stages} />
        </div>
      </div>
      <div className="rounded-lg border border-danger/30 bg-danger-soft p-3 text-sm">
        <p className="font-semibold text-danger">🚫 直接拒絕連線</p>
        <p className="mt-0.5 text-ink-80">
          {error.code === "TOKEN_INVALID"
            ? "憑證的簽章對不上（內容被改過或不是本系統簽發）。閘道在第 1 段就回 401，Jev、檢索與本地模型都沒有執行；這次嘗試已寫進拒絕並記錄。"
            : "沒有有效的身分憑證。閘道在第 1 段就回 401，Jev、檢索與本地模型都沒有執行。"}
        </p>
      </div>
    </div>
  );
}

function Checks({ checks }: { checks: GuardCheck[] }) {
  return (
    <ul className="mt-1.5 flex flex-col gap-1">
      {checks.map((c) => (
        <li key={c.key} className="flex items-start gap-2">
          <span className="mt-[5px] flex">
            <Dot state={c.ok === false ? "block" : c.warn ? "warn" : c.ok ? "ok" : "skip"} size="h-1.5 w-1.5" />
          </span>
          <span className="w-16 shrink-0 text-ink">{c.label}</span>
          <span className={c.ok === false ? "text-danger" : "text-ink-80"}>
            <span className={c.by === "Jev" ? "font-semibold text-warning" : "text-ink-48"}>{c.by}・</span>
            {c.detail}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Details({ route: r, calls }: { route: RouteResponse; calls: JevCallInfo[] }) {
  return (
    <div className="mt-4 flex flex-col gap-3 border-t border-hairline pt-3">
      {r.question && (
        <div>
          <p className="mb-1 text-ink-48">收到的文字（已遮蔽個資；之後各段與紀錄只看這個）</p>
          <p className="rounded-lg bg-tile px-2.5 py-1.5 font-mono text-[12px] text-[#e0e0e0]">{r.question}</p>
          {r.pii.length > 0 && (
            <p className="mt-1 text-ink-48">
              已遮蔽：
              {r.pii.map((p) => (
                <span key={p.code} className="ml-2 whitespace-nowrap">
                  {p.kind} → <b className="font-mono text-ink-80">{p.code}</b>
                </span>
              ))}
              <span className="ml-2">（原值不保留）</span>
            </p>
          )}
        </div>
      )}
      <div>
        <p className="mb-1 text-ink-48">第 1 段・閘道驗過的 JWT payload（第 3 段的 Metadata Filter 只照它產生）</p>
        <pre className="max-h-40 overflow-auto rounded-lg bg-tile px-2.5 py-1.5 font-mono text-[11px] text-[#e0e0e0]">
          {JSON.stringify(r.auth.token.claims, null, 2)}
        </pre>
      </div>
      {r.router.engine === "local" && r.question && (
        <div>
          <p className="mb-1 text-ink-48">第 2 段・本地分流：要交給哪個模組（{r.router.model}，{r.router.latency_ms} ms）</p>
          <div className="relative h-1.5 w-full max-w-sm overflow-hidden rounded-full bg-hairline">
            <div
              className={`h-full rounded-full ${r.confidence >= r.threshold ? "bg-success" : "bg-warning"}`}
              style={{ width: `${Math.round(r.confidence * 100)}%` }}
            />
            <div className="absolute top-0 h-full w-0.5 bg-ink/60" style={{ left: `${r.threshold * 100}%` }} />
          </div>
          <p className="mt-1 text-ink-48">
            信心 {r.confidence.toFixed(2)}／門檻 {r.threshold.toFixed(2)}・{r.gate_reason}
            {r.guard?.verdict ? `・Jev Choice：${VERDICT[r.guard.verdict]}` : ""}
          </p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {r.ranked.map((x) => (
              <span key={x.intent} className="rounded bg-card px-1.5 py-0.5 font-mono text-ink-80 ring-1 ring-hairline">
                {x.label} {x.prob.toFixed(2)}
              </span>
            ))}
          </div>
        </div>
      )}
      {calls.map((c) => (
        <JevSent key={c.stage} call={c} />
      ))}
    </div>
  );
}

const SENT_LABEL: Record<number, string> = {
  2: "Jev Choice・使用者的話",
  4: "Jev Noul・公開段落（雙重驗證）",
  5: "Jev Score・通過驗證的公開段落（評分）",
  6: "Jev Noul・留下的段落與作品資料（生成閘門）",
};

/** 送給 Jev 的東西：代號化內容、對照表（只留本機）、Jev 的回答、可展開的請求本文 */
function JevSent({ call }: { call: JevCallInfo }) {
  const [raw, setRaw] = useState(false);
  const codes = Object.entries(call.mapping);
  return (
    <div>
      <p className="mb-1 text-ink-48">
        第 {call.stage} 段送給 Jev（{SENT_LABEL[call.stage]}，代號化）・{kb(call.bytes)}・{call.latency_ms} ms
      </p>
      <div className="flex flex-col gap-0.5 rounded-lg bg-tile px-2.5 py-1.5 font-mono text-[12px] text-[#e0e0e0]">
        {call.sent.map((t, i) => (
          <p key={i} className={call.stage !== 2 && i > 0 ? "text-[11px] text-[#e0e0e0]/80" : ""}>
            {t}
          </p>
        ))}
      </div>
      {codes.length > 0 && (
        <p className="mt-1 text-ink-48">
          代號對照（只留在本機）：
          {codes.map(([code, e]) => (
            <span key={code} className="ml-1.5 whitespace-nowrap">
              <b className="font-mono text-ink-80">{code}</b>＝{String((e as { label?: string }).label)}
            </span>
          ))}
        </p>
      )}
      <div className="mt-1.5 grid max-w-xl grid-cols-[1fr_auto] gap-x-6 gap-y-0.5">
        {call.answers.map((a) => (
          <div key={a.label} className="contents">
            <span className="text-ink-80">{a.label}</span>
            <span className={`text-right font-mono ${a.alert ? "font-semibold text-danger" : "text-ink-48"}`}>{a.value}</span>
          </div>
        ))}
      </div>
      <button type="button" onClick={() => setRaw((x) => !x)} className="link mt-1">
        {raw ? "收起請求本文" : "看請求本文"}
      </button>
      {raw && (
        <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-tile p-2.5 font-mono text-[11px] text-[#e0e0e0]">
          {`POST /v1/systemone\n${JSON.stringify(call.request, null, 2)}`}
        </pre>
      )}
    </div>
  );
}
