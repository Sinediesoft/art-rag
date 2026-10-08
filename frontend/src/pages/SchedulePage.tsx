import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type ApiError,
  type ConstraintScore,
  type MemoryEvent,
  type PlannedWorkOrder,
  type ProductionOverview,
  type ScheduleKpis,
} from "../api/client";
import { useProductionOverview } from "../api/hooks";
import { ApiErrorMessage, ErrorMessage, Loading } from "../components/common/Feedback";
import { GanttChart, GanttLegend, orderColor, type GanttOrder } from "../components/schedule/GanttChart";
import { useScheduleSolve, type SolveState } from "../hooks/useScheduleSolve";
import { formatTaipei, seconds } from "../lib/format";

const STEPS = [
  {
    title: "從圖紙開立工單",
    body: "在圖紙頁依庫存缺口開立工單；途程（工序、機型、工時）來自圖紙的「加工製程」段落",
  },
  {
    title: "Timefold 求解",
    body: "工序是規劃實體、機台是清單變數；先建構初始解，再以局部搜尋最佳化交期、換線與完工時間",
  },
  {
    title: "寫回資料庫",
    body: "排程結果寫進工廠資料庫，Text-to-SQL 可以直接問「哪些工單會延遲」「VMC-01 排了什麼」",
  },
];

const LEVEL_LABEL: Record<string, string> = { hard: "硬", medium: "中", soft: "軟" };
const hours = (min: number) => (min / 60).toFixed(min % 60 ? 1 : 0);
/** "2026-10-08 14:33" → "10/08 14:33"；只有日期就只顯示日期 */
const fmt = (s: string | null | undefined) => (s ? s.slice(5).replace("-", "/") : "—");

/** "0hard/-120medium/-5000soft" → 中文三段 */
export function ScoreText({ score }: { score: string | null | undefined }) {
  if (!score) return <span className="text-ink-48">—</span>;
  const m = score.match(/(-?\d+)hard\/(-?\d+)medium\/(-?\d+)soft/);
  if (!m) return <span className="font-mono">{score}</span>;
  const [, h, md, sf] = m;
  return (
    <span className="tabular-nums">
      <span className={Number(h) < 0 ? "text-danger" : "text-success"}>硬 {h}</span>
      <span className="text-ink-48"> / </span>
      <span className={Number(md) < 0 ? "text-warning" : "text-success"}>中 {Number(md).toLocaleString()}</span>
      <span className="text-ink-48"> / </span>
      <span>軟 {Number(sf).toLocaleString()}</span>
    </span>
  );
}

export function MemoryNotice({ event }: { event: MemoryEvent | null | undefined }) {
  if (!event || (!event.released.length && !event.failed.length)) return null;
  return (
    <div className="rounded-xl border border-success/30 bg-success-soft/60 p-3 text-sm text-success">
      <b>記憶體管理</b>：{event.trigger}時{event.pool === "gpu" ? "顯示記憶體" : "記憶體"} {event.percent_before}%（門檻{" "}
      {event.threshold}%），已釋放{" "}
      {event.released.map((r) => r.label).join("、") || "—"}，降到 {event.percent_after}%。
      {event.kept.length > 0 && <span className="text-ink-80">　保留：{event.kept.join("、")}</span>}
      {event.failed.length > 0 && (
        <span className="text-warning">　無法釋放：{event.failed.map((f) => `${f.label}（${f.detail}）`).join("、")}</span>
      )}
    </div>
  );
}

export function SchedulePage() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useProductionOverview();
  const refresh = () => {
    for (const key of ["production-overview", "part-plan", "inventory-overview", "part-inventory", "diagnostics"]) {
      void qc.invalidateQueries({ queryKey: [key] });
    }
  };
  const { state, start } = useScheduleSolve(refresh);
  const [secondsLimit, setSecondsLimit] = useState(20);
  const [view, setView] = useState<"machine" | "order">("machine");
  const [stopping, setStopping] = useState(false);

  if (isLoading) return <Loading />;
  if (error || !data)
    return (
      <ApiErrorMessage
        error={error}
        title={(error as ApiError)?.code === "DATA_SCOPE_DENIED" ? undefined : "無法載入生產排程"}
      />
    );

  const solving = state.status === "starting" || state.status === "solving";
  const run = data.current;
  // 顯示優先順序：求解中的最新解 → 這次的結果 → 目前排程
  const live = state.progress ?? null;
  const shown = state.solution ?? live ?? run;
  const operations = shown?.operations ?? [];
  const planned: PlannedWorkOrder[] | null = state.solution?.work_orders ?? (live ? null : (run?.work_orders ?? null));
  const orders: GanttOrder[] = (planned ?? state.meta?.work_orders ?? data.work_orders).map((w) => ({
    wo_no: w.wo_no,
    part_name: w.part_name,
    priority: w.priority,
    qty: w.qty,
    due_min: w.due_min,
    late_min: "late_min" in w ? w.late_min : null,
  }));
  const axis = state.solution?.axis ?? state.meta?.axis ?? run?.axis ?? [];
  const kpis = (state.solution ?? live ?? run)?.kpis ?? null;
  const analysis = state.solution?.analysis ?? (live ? null : (run?.analysis ?? null));

  const stop = async () => {
    setStopping(true);
    await api.stopSchedule().finally(() => setStopping(false));
  };

  return (
    <div className="flex flex-col gap-8">
      <section className="card p-6 sm:p-10">
        <p className="t-eyebrow">生產排程 · Timefold Solver · 地端</p>
        <h1 className="t-display mt-2">
          把工單排上機台，
          <br className="sm:hidden" />
          交期與換線一起最佳化
        </h1>
        <p className="t-body mt-3 max-w-2xl text-ink-80">
          圖紙、工廠資料庫與排程是同一個系統：圖紙頁開立的工單依該圖紙的製程途程展開成工序，
          <b className="font-semibold text-ink">Timefold Solver</b> 決定每台機台的加工順序，結果寫回資料庫供 Text-to-SQL 查詢。
          排程只用數學最佳化、不用 AI 模型，資料全程不出本機。
        </p>
        <EngineLine data={data} />
        <div className="mt-6 flex flex-wrap items-center gap-2">
          {!solving ? (
            <>
              <button
                type="button"
                onClick={() => start({ seconds: secondsLimit, engine: "timefold" })}
                className="btn-primary"
                disabled={!data.work_orders.length}
              >
                {data.engine.available ? "開始排程（Timefold）" : "開始排程（簡易排程）"}
              </button>
              {data.engine.available && (
                <label className="flex items-center gap-1.5 rounded-full border border-hairline bg-canvas px-4 py-2.5 text-sm text-ink-80">
                  求解
                  <select
                    value={secondsLimit}
                    onChange={(e) => setSecondsLimit(Number(e.target.value))}
                    className="bg-transparent font-semibold text-ink outline-none"
                  >
                    {[10, 20, 30, 60].map((n) => (
                      <option key={n} value={n}>
                        {n} 秒
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <button
                type="button"
                onClick={() => start({ engine: "greedy" })}
                className="btn-ghost"
                title="只做一次交期優先派工，不做最佳化；用來和 Timefold 比較"
              >
                簡易排程（比較用）
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={stop}
              disabled={stopping || state.meta?.engine !== "timefold"}
              className="btn-dark"
            >
              {stopping ? "停止中…" : "提前結束，採用目前最佳解"}
            </button>
          )}
          <Link to="/" className="link ml-auto text-sm" title="例如「法蘭開 80 件 10/16 交」：一樣經過七段權限控管與確認卡">
            回智慧助理開立工單 →
          </Link>
        </div>
      </section>

      {state.error && <ErrorMessage title="排程失敗" message={state.error.message} code={state.error.code} requestId={state.error.request_id} />}
      {state.meta?.fallback_reason && (
        <div className="rounded-xl border border-warning/30 bg-warning-soft/60 p-3 text-sm text-warning">{state.meta.fallback_reason}</div>
      )}
      <MemoryNotice event={state.done?.memory} />

      {(solving || state.progress) && <LivePanel state={state} />}

      {!solving && !state.solution && run && (run.missing.length > 0 || run.removed.length > 0) && (
        <div className="rounded-xl border border-warning/30 bg-warning-soft/60 p-3 text-sm text-warning">
          {run.missing.length > 0 && <>有 {run.missing.length} 張工單尚未排入（{run.missing.join("、")}）。</>}
          {run.removed.length > 0 && <>{run.removed.join("、")} 已取消。</>}按「開始排程」重新排。
        </div>
      )}

      {kpis && <KpiTiles kpis={kpis} />}

      <section className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="t-tagline">
            {solving ? "目前最佳解（即時更新）" : state.solution ? "這次的排程結果" : run ? "目前排程" : "尚未排程"}
            {!solving && !state.solution && run && (
              <span className="ml-2 text-xs font-normal text-ink-48">
                {formatTaipei(run.created_at)} · {run.engine_label}
                {run.status === "stopped" && "（提前結束）"}
              </span>
            )}
          </h2>
          <div className="flex rounded-full border border-hairline bg-card p-1 text-sm">
            {(
              [
                ["machine", "機台"],
                ["order", "工單"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                onClick={() => setView(k)}
                className={`rounded-full px-4 py-1 transition ${view === k ? "bg-accent text-white" : "text-ink-80 hover:bg-parchment-deep"}`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        {operations.length > 0 ? (
          <>
            <GanttChart
              machines={data.machines}
              operations={operations}
              orders={orders}
              axis={axis}
              dayMinutes={data.calendar.day_minutes}
              view={view}
            />
            <GanttLegend view={view} />
          </>
        ) : (
          <p className="rounded-xl border border-dashed border-hairline bg-card p-8 text-center text-sm text-ink-80">
            {solving ? "建構初始解中…" : "還沒有排程結果。按「開始排程」把下方的工單排上機台。"}
          </p>
        )}
      </section>

      {analysis && (
        <ConstraintTable
          rows={analysis}
          weights={data.priority_weights}
          check={state.solution ? state.solution.score_check : live ? null : (run?.score_check ?? null)}
        />
      )}

      <WorkOrderTable data={data} planned={planned} onChanged={refresh} disabled={solving} />

      <section className="grid gap-3 sm:grid-cols-3">
        {STEPS.map((s, i) => (
          <div key={s.title} className="card p-5">
            <p className="font-mono text-xs font-semibold text-ink-48">0{i + 1}</p>
            <p className="font-semibold">{s.title}</p>
            <p className="mt-1 text-sm text-ink-80">{s.body}</p>
          </div>
        ))}
      </section>

      <MachineTable data={data} kpis={kpis} />
      {data.runs.length > 0 && <RunHistory data={data} />}
    </div>
  );
}

function EngineLine({ data }: { data: ProductionOverview }) {
  const e = data.engine;
  const p = data.problem as { n_work_orders: number; n_operations: number; n_machines: number; n_pinned: number };
  return (
    <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <span className={`inline-flex items-center gap-1.5 font-semibold ${e.available ? "text-success" : "text-warning"}`}>
        <span className={`inline-block h-2.5 w-2.5 rounded-full ${e.available ? "bg-success" : "bg-warning"}`} />
        {e.available ? `Timefold Solver ${e.version} 已啟動` : "Timefold 排程服務未啟動"}
      </span>
      <span className="text-ink-80">
        {p.n_work_orders} 張工單、{p.n_operations} 道工序（{p.n_pinned} 道生產中）、{p.n_machines} 台機台 · 排程起點{" "}
        {data.plan_start} 08:00
      </span>
      {!e.available && <span className="text-xs text-warning">{e.detail}</span>}
    </p>
  );
}

function LivePanel({ state }: { state: SolveState }) {
  const p = state.progress;
  const limit = (state.meta?.seconds ?? 0) * 1000;
  const elapsed = state.elapsedMs;
  const pct = limit ? Math.min(100, (elapsed / limit) * 100) : 100;
  const running = state.status === "solving" || state.status === "starting";
  return (
    <section className="card grid gap-4 p-5 md:grid-cols-[1.2fr_1fr]">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="font-semibold">
            {state.meta?.engine_label ?? "排程"}
            {state.meta?.engine_version && <span className="ml-1 font-mono text-xs text-ink-48">{state.meta.engine_version}</span>}
          </p>
          <p className="text-sm text-ink-80">
            {running ? (p?.phase ?? "準備中") : state.solution?.status === "stopped" ? "已提前結束" : "完成"} ·{" "}
            {seconds(elapsed)}
            {limit > 0 && ` / ${limit / 1000} 秒`}
          </p>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-parchment-deep">
          <div
            className={`h-full rounded-full transition-all ${running ? "bg-accent" : "bg-success"}`}
            style={{ width: `${running ? pct : 100}%` }}
          />
        </div>
        <p className="text-lg">
          <ScoreText score={p?.score} />
        </p>
        <p className="text-xs text-ink-48">
          找到更好的解 {p?.improvements ?? 0} 次
          {p?.initial_score && (
            <>
              {" "}
              · 初始解 <ScoreText score={p.initial_score} />
            </>
          )}
          {state.done && <> · 總耗時 {seconds(state.done.latency_ms.total)} · 外送 0</>}
        </p>
        {state.done && limit > 0 && elapsed < limit - 500 && state.solution?.status !== "stopped" && (
          <p className="text-xs text-ink-48">
            連續 {state.meta?.unimproved_seconds} 秒沒有找到更好的解，提前結束（不必等滿 {limit / 1000} 秒）
          </p>
        )}
        <p className="text-xs text-ink-48">
          分數越接近 0 越好：硬＝不可行（機型不符），中＝交期延遲（分鐘 × 急件權重），軟＝換線準備＋各工單完工時間
        </p>
      </div>
      <ScoreSparkline history={state.history} />
    </section>
  );
}

/** 中分數（交期延遲）與軟分數隨時間的變化 */
function ScoreSparkline({ history }: { history: SolveState["history"] }) {
  if (history.length < 2)
    return <div className="grid place-items-center text-xs text-ink-48">分數變化會顯示在這裡</div>;
  const W = 320;
  const H = 96;
  const maxT = Math.max(...history.map((h) => h.elapsed_ms), 1);
  const line = (key: "medium" | "soft") => {
    const vals = history.map((h) => h[key]);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const span = hi - lo || 1;
    return history
      .map((h, i) => `${i ? "L" : "M"}${((h.elapsed_ms / maxT) * (W - 8) + 4).toFixed(1)},${(H - 8 - ((h[key] - lo) / span) * (H - 20)).toFixed(1)}`)
      .join(" ");
  };
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-24 w-full">
        <path d={line("soft")} fill="none" stroke="#0066cc" strokeWidth="2" />
        <path d={line("medium")} fill="none" stroke="#b64400" strokeWidth="2" strokeDasharray="5 3" />
      </svg>
      <p className="flex gap-4 text-xs text-ink-48">
        <span className="text-warning">┅ 中（交期延遲）</span>
        <span className="text-accent">━ 軟（換線＋完工）</span>
        <span>越往上越好</span>
      </p>
    </div>
  );
}

function KpiTiles({ kpis }: { kpis: ScheduleKpis }) {
  const tiles: [string, string, string, string][] = [
    [
      "準時率",
      kpis.on_time_rate != null ? `${Math.round(kpis.on_time_rate * 100)}%` : "—",
      `${kpis.n_work_orders - kpis.n_late}／${kpis.n_work_orders} 張工單準時`,
      kpis.n_late ? "text-warning" : "text-success",
    ],
    [
      "延遲",
      kpis.n_late ? `${hours(kpis.total_late_min)} 小時` : "0",
      kpis.n_late ? `${kpis.n_late} 張工單，以上班時間計` : "全部在交期前完工",
      kpis.n_late ? "text-danger" : "text-success",
    ],
    ["換線準備", `${hours(kpis.total_setup_min)} 小時`, `${kpis.n_setups} 次換線`, "text-ink"],
    ["全部完工", fmt(kpis.finish_at?.slice(0, 10)), kpis.finish_at ? `${kpis.finish_at.slice(11)}（含委外）` : "", "text-ink"],
  ];
  return (
    <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {tiles.map(([label, v, sub, color]) => (
        <div key={label} className="card px-4 py-3">
          <p className="text-xs text-ink-48">{label}</p>
          <p className={`font-display text-2xl font-semibold tabular-nums ${color}`}>{v}</p>
          <p className="text-xs text-ink-80">{sub}</p>
        </div>
      ))}
    </section>
  );
}

function ConstraintTable({
  rows,
  weights,
  check,
}: {
  rows: ConstraintScore[];
  weights: Record<string, number>;
  check: boolean | null | undefined;
}) {
  return (
    <section>
      <h2 className="t-tagline mb-3">限制條件與分數</h2>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[560px] text-sm">
          <thead className="t-caption-strong text-left text-ink-48">
            <tr>
              <th className="px-3 py-2.5 font-semibold">層級</th>
              <th className="px-3 py-2.5 font-semibold">限制條件</th>
              <th className="px-3 py-2.5 text-right font-semibold">分數</th>
              <th className="px-3 py-2.5 text-right font-semibold">次數</th>
              <th className="px-3 py-2.5 font-semibold">說明</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.constraint} className="border-t border-hairline">
                <td className="px-3 py-2.5">
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                      r.level === "hard" ? "bg-danger-soft text-danger" : r.level === "medium" ? "bg-warning-soft text-warning" : "bg-parchment-deep text-ink-80"
                    }`}
                  >
                    {LEVEL_LABEL[r.level]}
                  </span>
                </td>
                <td className="px-3 py-2.5">{r.constraint}</td>
                <td className={`px-3 py-2.5 text-right tabular-nums ${r.score < 0 && r.level !== "soft" ? "text-danger" : ""}`}>
                  {r.score.toLocaleString()}
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums">{r.matches}</td>
                <td className="px-3 py-2.5 text-ink-80">{r.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-ink-48">
        Timefold 先比硬分數、再比中分數、最後比軟分數。急件延遲 1 分鐘＝一般工單延遲 {weights["急件"] ?? 3} 分鐘。
        明細由後端用同一套規則重算（app/scheduling/solution.py）
        {check === true && <span className="font-semibold text-success">，總分與 Timefold 一致 ✓</span>}
        {check === false && <span className="font-semibold text-danger">，總分與 Timefold 不一致（請檢查兩邊規則）</span>}。
      </p>
    </section>
  );
}

function WorkOrderTable({
  data,
  planned,
  onChanged,
  disabled,
}: {
  data: ProductionOverview;
  planned: PlannedWorkOrder[] | null;
  onChanged: () => void;
  disabled: boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const byNo = new Map((planned ?? []).map((w) => [w.wo_no, w]));
  const orders = data.work_orders.map((w) => ({ wo_no: w.wo_no, part_name: w.part_name, priority: w.priority, qty: w.qty, due_min: w.due_min }));
  const cancel = async (woNo: string) => {
    setBusy(woNo);
    await api.cancelWorkOrder(woNo).finally(() => setBusy(null));
    onChanged();
  };
  return (
    <section>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="t-tagline">待排程的工單</h2>
        <span className="text-xs text-ink-48">
          急件權重 ×{data.priority_weights["急件"]} · 生產中的工單從目前工序接著排
        </span>
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="t-caption-strong whitespace-nowrap text-left text-ink-48">
            <tr>
              <th className="px-3 py-2.5 font-semibold">工單</th>
              <th className="px-3 py-2.5 font-semibold">品名</th>
              <th className="px-3 py-2.5 text-right font-semibold">數量</th>
              <th className="px-3 py-2.5 font-semibold">狀態</th>
              <th className="px-3 py-2.5 font-semibold">交期</th>
              <th className="px-3 py-2.5 font-semibold">排程完工</th>
              <th className="px-3 py-2.5 font-semibold" />
            </tr>
          </thead>
          <tbody>
            {data.work_orders.map((w) => {
              const p = byNo.get(w.wo_no);
              return (
                <tr key={w.wo_no} className="border-t border-hairline">
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <span className="mr-1.5 inline-block h-2.5 w-2.5 rounded-sm" style={{ background: orderColor(orders, w.wo_no) }} />
                    <span className="font-mono text-xs">{w.wo_no}</span>
                    {w.priority === "急件" && (
                      <span className="ml-1.5 rounded-full bg-danger-soft px-1.5 py-0.5 text-[11px] font-semibold text-danger">急件</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    <Link to={`/drawings/${w.part_id}`} className="hover:text-accent hover:underline">
                      {w.part_name}
                    </Link>
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums">{w.qty}</td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-xs">
                    {w.status}
                    <span className={`ml-1 rounded-full px-1.5 py-0.5 ${w.source === "系統開立" ? "bg-accent-soft text-ink" : "bg-parchment-deep text-ink-80"}`}>
                      {w.source === "系統開立" ? "圖紙頁開立" : "既有"}
                    </span>
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-xs tabular-nums">{fmt(w.due_on)}</td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-xs tabular-nums">
                    {p ? (
                      <span className={p.late_min ? "font-semibold text-danger" : "text-success"}>
                        {fmt(p.end_at)}
                        {p.late_min ? `（延遲 ${hours(p.late_min)} 小時）` : "（準時）"}
                      </span>
                    ) : (
                      <span className="text-ink-48">尚未排程</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    {w.source === "系統開立" && (
                      <button
                        type="button"
                        disabled={disabled || busy === w.wo_no}
                        onClick={() => cancel(w.wo_no)}
                        className="rounded-full border border-danger/40 px-3 py-1 text-xs text-danger transition hover:bg-danger-soft disabled:opacity-40"
                      >
                        取消
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {data.skipped.length > 0 && (
        <p className="mt-2 text-xs text-ink-48">
          不排程：{data.skipped.map((s) => `${s.wo_no}（${s.reason}）`).join("；")}
        </p>
      )}
    </section>
  );
}

function MachineTable({ data, kpis }: { data: ProductionOverview; kpis: ScheduleKpis | null }) {
  return (
    <section>
      <h2 className="t-tagline mb-3">{data.machines.length} 台機台</h2>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {data.machines.map((m) => {
          const u = kpis?.utilization?.[m.machine_id] ?? null;
          return (
            <div key={m.machine_id} className="rounded-lg border border-hairline bg-card px-3 py-2.5 text-sm">
              <p className="font-mono font-semibold">{m.machine_id}</p>
              <p className="text-xs text-ink-80">
                {m.machine_type}・{m.name}・{m.site}
              </p>
              {u !== null && (
                <div className="mt-1.5 flex items-center gap-2">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-parchment-deep">
                    <div className="h-full rounded-full bg-accent" style={{ width: `${Math.round(u * 100)}%` }} />
                  </div>
                  <span className="text-xs tabular-nums text-ink-48">{Math.round(u * 100)}%</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-ink-48">
        行事曆：週一至週五 {data.calendar.shifts.map((s) => s.join("–")).join("、")}
        {data.calendar.holidays.map((h) => `；${h.date} ${h.name}`)}。
        百分比＝排程期間的忙碌比例。機台與途程設定在 <code className="font-mono">kb/production/</code>。
      </p>
    </section>
  );
}

function RunHistory({ data }: { data: ProductionOverview }) {
  return (
    <section>
      <h2 className="t-tagline mb-3">排程紀錄</h2>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="t-caption-strong text-left text-ink-48">
            <tr>
              <th className="px-3 py-2.5 font-semibold">時間</th>
              <th className="px-3 py-2.5 font-semibold">引擎</th>
              <th className="px-3 py-2.5 font-semibold">工單／工序</th>
              <th className="px-3 py-2.5 font-semibold">初始解 → 最佳解</th>
              <th className="px-3 py-2.5 font-semibold">準時</th>
              <th className="px-3 py-2.5 font-semibold">耗時</th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map((r) => (
              <tr key={r.run_id} className="border-t border-hairline align-top">
                <td className="whitespace-nowrap px-3 py-2.5 text-xs">{formatTaipei(r.created_at)}</td>
                <td className="whitespace-nowrap px-3 py-2.5 text-xs">
                  {r.engine === "timefold" ? `Timefold ${r.engine_version ?? ""}` : "簡易排程"}
                  {r.status === "stopped" && "（提前結束）"}
                </td>
                <td className="px-3 py-2.5 text-xs">
                  {r.n_work_orders}／{r.n_operations}
                </td>
                <td className="px-3 py-2.5 text-xs">
                  {r.initial_score && (
                    <>
                      <ScoreText score={r.initial_score} /> →{" "}
                    </>
                  )}
                  <ScoreText score={r.score} />
                </td>
                <td className="px-3 py-2.5 text-xs">
                  {r.kpis.on_time_rate != null ? `${Math.round(r.kpis.on_time_rate * 100)}%` : "—"}
                </td>
                <td className="px-3 py-2.5 text-xs">{seconds(r.solve_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
