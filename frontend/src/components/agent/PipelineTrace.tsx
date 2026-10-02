import { useState } from "react";
import type { RouteResponse } from "../../api/client";
import type { DoneEvent, ErrorEvent, GuardCheck, JevCallInfo, PostFilterInfo, SourcesEvent } from "../../api/sse";

/**
 * 五段防護的處理過程（docs/adr/012）：收合時是一行流程列「RBAC › Jev 護欄 › 檢索 › Jev 過濾 › 地端生成」＋摘要，
 * 點開看每一段的檢查、Metadata Filter、每個候選段落的去留、意圖機率與門檻、送給 Jev 的代號化內容與請求本文。
 * 第 1、2 段來自 /agent/route；第 3～5 段來自分派到的模組（問答的 sources／done 事件）。
 */

export type StageState = "ok" | "warn" | "block" | "skip" | "doing" | "todo" | "handoff";

export interface Stage {
  key: "rbac" | "guard" | "retrieve" | "post" | "gen";
  title: string;
  short: string;
  state: StageState;
  detail: string;
  checks?: GuardCheck[];
}

/** 分派到的模組回報的進度（只有問答有第 3～5 段的即時資訊） */
export interface ModuleProgress {
  sources?: SourcesEvent | null;
  done?: DoneEvent | null;
  error?: ErrorEvent | null;
  /** 圖紙查找：不在資料範圍、檢索時就濾掉的張數與 filter */
  parts?: { found: number; hidden: number; filter: string | null } | null;
}

const kb = (b: number) => `${(b / 1024).toFixed(1)} KB`;
const NAMES: Record<Stage["key"], [string, string]> = {
  rbac: ["接收輸入・RBAC 權限檢查", "RBAC"],
  guard: ["前置防禦・Jev 第一層護欄", "Jev 護欄"],
  retrieve: ["檢索・Metadata Filter 向量檢索", "檢索"],
  post: ["後置過濾・Jev 第二層過濾", "Jev 過濾"],
  gen: ["最終生成・地端 LLM", "地端生成"],
};
const GATE_TEXT: Record<RouteResponse["gate"], string> = {
  direct: "直接執行",
  confirm: "先確認",
  modify: "試算＋確認卡",
  clarify: "請你選",
  out_of_scope: "超出範圍",
};

function stage(key: Stage["key"], state: StageState, detail: string, extra: Partial<Stage> = {}): Stage {
  return { key, title: NAMES[key][0], short: NAMES[key][1], state, detail, ...extra };
}

/** 問答會不會走 /chat（第 3～5 段有即時資訊） */
export function isChat(route: RouteResponse) {
  const d = route.dispatch as { artwork_id?: string | null; part_id?: string | null };
  return (route.intent === "art_qa" && !!d.artwork_id) || (route.intent === "drawing_qa" && !!d.part_id);
}

function postDetail(pf: PostFilterInfo) {
  const how: string[] = [];
  if (pf.call) how.push(`雲端 Jev 檢查 ${pf.cloud} 段公開段落（代號化，外送 ${kb(pf.call.bytes)}）`);
  if (pf.fallback_reason) how.push(`Jev 不能用（${pf.fallback_reason}），改在地端過濾`);
  if (pf.local)
    how.push(
      pf.mode === "jev" && !pf.fallback_reason
        ? `${pf.local} 段內部／機密段落不出廠，改在地端過濾（規則掃描＋相似度排序）`
        : `地端規則掃描＋相似度排序 ${pf.local} 段（Jev 未使用）`,
    );
  const dropped = pf.dropped.length ? `、${pf.dropped.length} 段不相關` : "";
  return `${how.join("；")} → 移除 ${pf.injected.length} 段夾帶指令${dropped} → 保留 ${pf.kept} 段`;
}

export function buildStages(route: RouteResponse, mod: ModuleProgress): Stage[] {
  const r = route;
  const d = r.dispatch as { part_label?: string; artwork_label?: string; part_id?: string | null };
  const out: Stage[] = [];

  // ---- 第 1 段
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
  const rb = r.rbac;
  out.push(
    stage(
      "rbac",
      !rb.passed ? "block" : rb.pending || rb.checks.some((c) => c.warn) ? "warn" : "ok",
      `${photo}${routed}${pii} → ${
        rb.pending ? "等你選擇後再檢查範圍與動作" : rb.passed ? "身分、資料範圍、動作權限都通過" : `未通過：${rb.reason}`
      }`,
      { checks: rb.checks },
    ),
  );

  // ---- 第 2 段
  const g = r.guard;
  if (!g) out.push(stage("guard", "skip", "RBAC 沒過，沒有送給 Jev"));
  else if (g.engine === "skip") out.push(stage("guard", "skip", g.skipped ?? "沒有文字要掃描"));
  else {
    const judge = g.call
      ? `雲端 Jev（${g.call.model}・${g.call.latency_ms} ms・外送 ${kb(g.call.bytes)} 代號化文字）`
      : `地端規則（${g.fallback_reason ? `Jev 不能用：${g.fallback_reason}；` : ""}外送 0 B）`;
    out.push(
      stage(
        "guard",
        !g.passed ? "block" : g.checks.some((c) => c.warn) || g.fallback_reason ? "warn" : "ok",
        `${judge} → ${g.passed ? "沒有發現惡意注入" : `未通過：${g.reason}`}`,
        { checks: g.checks },
      ),
    );
  }

  // ---- 第 3～5 段
  if (r.outcome !== "pass") {
    out.push(
      stage("retrieve", "skip", r.outcome === "blocked_rbac" ? "沒有檢索" : "Jev 護欄擋下，沒有檢索"),
      stage("post", "skip", "沒有執行"),
      stage("gen", "skip", "沒有碰到任何資料與模型"),
    );
    return out;
  }
  if (r.gate === "clarify") {
    out.push(stage("retrieve", "skip", "等你選擇"), stage("post", "skip", "等你選擇"), stage("gen", "skip", "等你選擇"));
    return out;
  }
  const reply = (detail: string) => stage("gen", "ok", detail, { title: "直接回覆", short: "回覆" });
  const handoff = (detail: string, title: string, short: string) => stage("gen", "handoff", detail, { title, short });
  const filterText = r.rbac.filter?.text;

  if (r.photo?.kind === "unknown" && !r.question) {
    out.push(stage("retrieve", "skip", "照片比對不到知識庫，沒有檢索"), stage("post", "skip", "沒有段落要過濾"), reply("請你補一句說明"));
  } else if (r.intent === "out_of_scope") {
    out.push(stage("retrieve", "skip", "不需檢索"), stage("post", "skip", "沒有段落要過濾"), reply("固定回覆系統能做的事"));
  } else if (r.intent === "system") {
    out.push(stage("retrieve", "skip", "不需檢索"), stage("post", "skip", "沒有段落要過濾"), reply("讀取記憶體、服務狀態與攔截紀錄"));
  } else if (isChat(r)) {
    const { sources, done, error } = mod;
    const pf = sources?.post_filter;
    const mfg = r.intent === "drawing_qa";
    if (error && !sources) {
      out.push(stage("retrieve", "block", error.message), stage("post", "skip", "沒有執行"), stage("gen", "skip", "沒有執行"));
    } else {
      out.push(
        sources
          ? stage("retrieve", "ok", `bge-m3 向量檢索・${sources.filter?.text ?? filterText} → ${sources.candidates ?? sources.sources.length} 段候選`)
          : stage("retrieve", "doing", `bge-m3 向量檢索・${filterText ?? ""}`),
        pf
          ? stage("post", pf.injected.length ? "warn" : "ok", postDetail(pf), { checks: pf.checks })
          : stage("post", sources ? "skip" : "todo", sources ? "沒有段落要過濾" : "等檢索結果"),
        done
          ? stage(
              "gen",
              "ok",
              `地端 LLM（${done.model}）只依 ${sources?.sources.length ?? 0} 段乾淨上下文生成${mfg ? "（機密圖紙全程地端）" : ""}・首字 ${((done.latency_ms.first_token ?? 0) / 1000).toFixed(1)} 秒`,
            )
          : error
            ? stage("gen", "block", error.message)
            : stage("gen", sources ? "doing" : "todo", `地端 LLM 只依第 4 段留下的段落生成${mfg ? "（機密圖紙全程地端）" : ""}`),
      );
    }
  } else if (r.intent === "art_search" || r.intent === "art_qa") {
    out.push(
      stage("retrieve", "ok", `Chinese-CLIP＋bge-m3 以文搜畫・${filterText ?? 'domain = "畫作" AND level IN ("公開")'}`),
      stage("post", "skip", "搜尋結果直接列出畫作，不放進生成上下文"),
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
      stage("post", "skip", "搜尋結果直接列出圖紙，不放進生成上下文"),
      stage("gen", "skip", "直接列出圖紙，不經生成"),
    );
  } else if (r.intent === "data_query") {
    out.push(
      stage("retrieve", "handoff", "地端 LLM 產生 SQL → 唯讀檢查 → SQLite 執行（資料庫查詢，不經向量檢索）", {
        title: "檢索・唯讀資料庫查詢",
        short: "查詢",
      }),
      stage("post", "skip", "查詢結果是資料庫數字，不經文件過濾"),
      handoff("地端 LLM 依查詢結果回答（進度見下方）", "最終生成・地端 LLM", "地端生成"),
    );
  } else if (r.intent === "reconstruct") {
    out.push(
      d.part_id
        ? stage("retrieve", "ok", `讀取〈${d.part_label}〉的圖紙（第 1 段已允許）`, { title: "檢索・讀取圖紙", short: "讀圖紙" })
        : stage("retrieve", "skip", "等你選圖紙", { title: "檢索・讀取圖紙", short: "讀圖紙" }),
      stage("post", "skip", "圖紙影像直接交給 Ortho2CAD，不經文字過濾"),
      handoff("等你確認後交給 Ortho2CAD（地端）", "Ortho2CAD 3D 重建", "3D 重建"),
    );
  } else if (r.intent === "schedule") {
    out.push(
      stage("retrieve", "skip", "不需檢索"),
      stage("post", "skip", "沒有段落要過濾"),
      handoff("等你確認後交給 Timefold（地端）", "Timefold 排程", "排程"),
    );
  } else if (r.intent === "modify") {
    out.push(
      stage("retrieve", "skip", "不需檢索（試算時讀取目前資料）"),
      stage("post", "skip", "沒有段落要過濾"),
      handoff("參數抽取 → 範圍／欄位／上限 → 副本試算 → 額度判斷", "修改資料流程", "試算"),
    );
  }
  return out;
}

function summaryOf(route: RouteResponse, mod: ModuleProgress) {
  if (route.blocked)
    return `${route.blocked.stage === 1 ? "RBAC" : "Jev 護欄"}擋下：${route.blocked.rule}・已記錄 ${route.blocked.log_no}`;
  if (route.gate === "clarify") return "要做什麼不確定 → 請你選";
  const removed = mod.sources?.post_filter?.injected.length;
  return `${route.intent_label}・${removed ? `移除 ${removed} 段間接注入・` : ""}${GATE_TEXT[route.gate]}`;
}

const DOT: Record<StageState, string> = {
  ok: "bg-jade",
  warn: "bg-amber",
  block: "bg-seal",
  handoff: "bg-steel",
  skip: "",
  todo: "",
  doing: "",
};

function Dot({ state, size = "h-2 w-2" }: { state: StageState; size?: string }) {
  if (state === "doing")
    return <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-line border-t-seal" />;
  if (state === "skip" || state === "todo")
    return <span className={`${size} inline-block shrink-0 rounded-full border border-ink-faint/60`} />;
  return <span className={`${size} inline-block shrink-0 rounded-full ${DOT[state]}`} />;
}

export function PipelineTrace({ route, mod }: { route: RouteResponse; mod: ModuleProgress }) {
  const [open, setOpen] = useState(false);
  const stages = buildStages(route, mod);
  const calls = [route.guard?.call, mod.sources?.post_filter?.call].filter(Boolean) as JevCallInfo[];
  const egress = calls.reduce((n, c) => n + c.bytes, 0);
  const blocked = !!route.blocked;

  return (
    <div className="rounded-xl border border-line bg-paper/60 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full flex-col items-start gap-1.5 p-3 text-left"
      >
        <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
          <span className="mr-1 font-bold tracking-wide text-ink-faint">處理過程</span>
          {stages.map((s, i) => (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              {i > 0 && <span className="text-ink-faint/60">›</span>}
              <Dot state={s.state} />
              <span
                className={
                  s.state === "block"
                    ? "font-bold text-seal"
                    : s.state === "skip" || s.state === "todo"
                      ? "text-ink-faint/80"
                      : "font-medium text-ink"
                }
              >
                {s.short}
              </span>
            </span>
          ))}
          <span className={`ml-1 text-ink-faint transition ${open ? "rotate-90" : ""}`}>›</span>
        </span>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className={blocked ? "font-bold text-seal" : "text-ink-soft"}>{summaryOf(route, mod)}</span>
          <span
            className={`rounded-full px-2 py-0.5 font-mono font-bold ${egress ? "bg-[#fde8df] text-[#b5481f]" : "bg-jade-soft text-jade"}`}
            title={egress ? "只送遮蔽個資、代號化後的文字與公開段落；照片、原始名稱、機密段落都不送" : "全程在本機"}
          >
            {egress ? `外送 ${kb(egress)} → Jev${calls.length > 1 ? `（${calls.length} 次）` : ""}` : "外送 0 B・全程地端"}
          </span>
          <span className="text-ink-faint">點開看每一段的檢查</span>
        </span>
      </button>

      {open && (
        <div className="border-t border-line px-3 pb-3 pt-3">
          <ol className="flex flex-col gap-3">
            {stages.map((s, i) => (
              <li key={s.key} className="flex gap-2.5">
                <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-card font-mono text-[11px] font-bold text-ink-soft ring-1 ring-line">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <Dot state={s.state} />
                    <span
                      className={`font-bold ${s.state === "block" ? "text-seal" : s.state === "skip" || s.state === "todo" ? "text-ink-faint" : "text-ink"}`}
                    >
                      {s.title}
                    </span>
                  </span>
                  <span className="mt-0.5 block text-ink-soft">{s.detail}</span>
                  {s.checks && s.checks.length > 0 && <Checks checks={s.checks} />}
                </span>
              </li>
            ))}
          </ol>
          <Details route={route} calls={calls} />
        </div>
      )}
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
          <span className={c.ok === false ? "text-seal" : "text-ink-soft"}>
            <span className={c.by === "Jev" ? "font-bold text-[#b5481f]" : "text-ink-faint"}>{c.by}・</span>
            {c.detail}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Details({ route: r, calls }: { route: RouteResponse; calls: JevCallInfo[] }) {
  return (
    <div className="mt-4 flex flex-col gap-3 border-t border-line pt-3">
      {r.question && (
        <div>
          <p className="mb-1 text-ink-faint">收到的文字（已遮蔽個資；之後各段與紀錄只看這個）</p>
          <p className="rounded bg-code px-2 py-1 font-mono text-[12px] text-[#d7e3ee]">{r.question}</p>
          {r.pii.length > 0 && (
            <p className="mt-1 text-ink-faint">
              已遮蔽：
              {r.pii.map((p) => (
                <span key={p.code} className="ml-2 whitespace-nowrap">
                  {p.kind} → <b className="font-mono text-ink-soft">{p.code}</b>
                </span>
              ))}
              <span className="ml-2">（原值不保留）</span>
            </p>
          )}
        </div>
      )}
      {r.router.engine === "local" && r.question && (
        <div>
          <p className="mb-1 text-ink-faint">第 1 段・本地分流的意圖機率（{r.router.model}，{r.router.latency_ms} ms）</p>
          <div className="relative h-1.5 w-full max-w-sm overflow-hidden rounded-full bg-line">
            <div
              className={`h-full rounded-full ${r.confidence >= r.threshold ? "bg-jade" : "bg-amber"}`}
              style={{ width: `${Math.round(r.confidence * 100)}%` }}
            />
            <div className="absolute top-0 h-full w-0.5 bg-ink/60" style={{ left: `${r.threshold * 100}%` }} />
          </div>
          <p className="mt-1 text-ink-faint">
            信心 {r.confidence.toFixed(2)}／門檻 {r.threshold.toFixed(2)}・{r.gate_reason}
          </p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {r.ranked.map((x) => (
              <span key={x.intent} className="rounded bg-card px-1.5 py-0.5 font-mono text-ink-soft ring-1 ring-line">
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

/** 送給 Jev 的東西：代號化內容、對照表（只留本機）、Jev 的回答、可展開的請求本文 */
function JevSent({ call }: { call: JevCallInfo }) {
  const [raw, setRaw] = useState(false);
  const codes = Object.entries(call.mapping);
  return (
    <div>
      <p className="mb-1 text-ink-faint">
        第 {call.stage} 段送給 Jev（{call.stage === 2 ? "使用者的話" : "公開段落"}，代號化）・{kb(call.bytes)}・{call.latency_ms} ms
      </p>
      <div className="flex flex-col gap-0.5 rounded bg-code px-2 py-1 font-mono text-[12px] text-[#d7e3ee]">
        {call.sent.map((t, i) => (
          <p key={i} className={call.stage === 4 && i > 0 ? "text-[11px] text-[#d7e3ee]/80" : ""}>
            {t}
          </p>
        ))}
      </div>
      {codes.length > 0 && (
        <p className="mt-1 text-ink-faint">
          代號對照（只留在本機）：
          {codes.map(([code, e]) => (
            <span key={code} className="ml-1.5 whitespace-nowrap">
              <b className="font-mono text-ink-soft">{code}</b>＝{String((e as { label?: string }).label)}
            </span>
          ))}
        </p>
      )}
      <div className="mt-1.5 grid max-w-xl grid-cols-[1fr_auto] gap-x-6 gap-y-0.5">
        {call.answers.map((a) => (
          <div key={a.label} className="contents">
            <span className="text-ink-soft">{a.label}</span>
            <span className={`text-right font-mono ${a.alert ? "font-bold text-seal" : "text-ink-faint"}`}>{a.value}</span>
          </div>
        ))}
      </div>
      <button type="button" onClick={() => setRaw((x) => !x)} className="mt-1 text-ink-faint underline hover:text-ink">
        {raw ? "收起請求本文" : "看請求本文"}
      </button>
      {raw && (
        <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded bg-code p-2 font-mono text-[11px] text-[#d7e3ee]">
          {`POST /v1/systemone\n${JSON.stringify(call.request, null, 2)}`}
        </pre>
      )}
    </div>
  );
}
