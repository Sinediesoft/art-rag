import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type WorkOrderCreated } from "../../api/client";
import { usePartPlan } from "../../api/hooks";

const fmt = (s: string | null | undefined) => (s ? `${s.slice(5, 10).replace("-", "/")}${s.length > 10 ? ` ${s.slice(11)}` : ""}` : "—");

/**
 * 圖紙頁的「生產工單」卡：看這張圖紙的製程途程與工單排程，依庫存缺口開立新工單，交給生產排程。
 * 圖紙（途程）、資料庫（庫存、工單）、排程在這裡接起來。
 */
export function PartProductionCard({ partId }: { partId: string }) {
  const qc = useQueryClient();
  const { data: plan, error } = usePartPlan(partId);
  const [qty, setQty] = useState(0);
  const [due, setDue] = useState("");
  const [priority, setPriority] = useState<"一般" | "急件">("一般");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<WorkOrderCreated | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [showRouting, setShowRouting] = useState(false);

  // 建議值帶進表單（換圖紙時重新帶）
  useEffect(() => {
    if (!plan) return;
    setQty(plan.suggestion.qty);
    setDue(plan.suggestion.due_on);
    setPriority(plan.suggestion.priority);
    setCreated(null);
    setFailure(null);
  }, [plan?.part_id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error || !plan) return null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFailure(null);
    try {
      const wo = await api.createWorkOrder({ part_id: partId, qty, due_on: due, priority, note: note.trim() || null });
      setCreated(wo);
      setNote("");
      for (const key of ["part-plan", "production-overview", "inventory-overview", "part-inventory"]) {
        void qc.invalidateQueries({ queryKey: [key] });
      }
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };


  return (
    <section className="rounded-xl border border-steel/25 bg-card p-4 text-sm">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="font-bold">生產工單與排程</h2>
        <Link to="/schedule" className="text-xs font-bold text-steel hover:underline">
          生產排程 →
        </Link>
      </div>

      {plan.has_routing ? (
        <button
          type="button"
          onClick={() => setShowRouting((v) => !v)}
          className="mb-2 flex w-full flex-wrap items-center gap-1 text-left text-xs text-ink-soft"
        >
          <span className="font-bold text-ink-faint">途程</span>
          {plan.routing.map((o, i) => (
            <span key={o.op_seq} className="inline-flex items-center gap-1">
              {i > 0 && <span className="text-ink-faint">→</span>}
              <span
                className={`rounded px-1.5 py-0.5 ${o.kind === "委外" ? "border border-dashed border-steel/40 text-steel" : "bg-steel-soft text-steel-deep"}`}
              >
                {o.kind === "委外" ? `${o.name.slice(0, 6)}（委外）` : o.machine_type}
              </span>
            </span>
          ))}
          <span className="ml-1 text-steel underline">{showRouting ? "收合" : "明細"}</span>
        </button>
      ) : (
        <p className="mb-2 text-xs text-amber">這張圖紙還沒有製程途程（kb/production/routings/），無法排程。</p>
      )}
      {showRouting && (
        <ol className="mb-3 flex flex-col gap-1 rounded-lg bg-paper-deep/50 p-2 text-xs">
          {plan.routing.map((o) => (
            <li key={o.op_seq} className="flex flex-wrap gap-x-2">
              <span className="font-mono text-ink-faint">{o.op_seq}</span>
              <span className="font-medium">{o.name}</span>
              {o.kind === "委外" ? (
                <span className="text-steel">委外 {o.outsource_days} 個工作天</span>
              ) : (
                <span className="text-ink-faint">
                  {o.machine_type}（{o.machines.join("、")}）· 準備 {o.setup_min} 分＋每件 {o.run_min_per_pc} 分
                </span>
              )}
            </li>
          ))}
          <li className="text-ink-faint">工序與「加工製程」段落一一對應；工時為示範估計值</li>
        </ol>
      )}

      {plan.work_orders.length > 0 && (
        <ul className="mb-3 flex flex-col gap-0.5">
          {plan.work_orders.map((w) => (
            <li key={w.wo_no} className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-mono text-xs">{w.wo_no}</span>
              <span className="text-xs text-ink-soft">
                {w.status}・{w.source === "系統開立" ? "圖紙頁開立" : "既有"}
                {w.priority === "急件" && "・急件"}
              </span>
              <span>{w.qty} 件</span>
              <span className="text-xs text-ink-faint">交期 {fmt(w.due_on)}</span>
              {w.plan ? (
                <span className={`text-xs ${w.plan.late_min ? "font-bold text-seal" : "text-jade"}`}>
                  排程完工 {fmt(w.plan.end_at)}
                  {w.plan.late_min ? `・延遲 ${(w.plan.late_min / 60).toFixed(1)} 小時` : "・準時"}
                </span>
              ) : (
                <span className="text-xs text-amber">尚未排程</span>
              )}
            </li>
          ))}
          {plan.skipped.map((s) => (
            <li key={s.wo_no} className="text-xs text-ink-faint">
              <span className="font-mono">{s.wo_no}</span> {s.reason}（不佔機台）
            </li>
          ))}
        </ul>
      )}

      {plan.has_routing && (
        <form onSubmit={submit} className="rounded-lg border border-line bg-paper/60 p-3">
          <p className="mb-2 text-xs text-ink-soft">
            <b className="text-ink">建議</b>：{plan.suggestion.reason}
          </p>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <label className="flex flex-col gap-0.5 text-xs text-ink-faint">
              數量（件）
              <input
                type="number"
                min={1}
                max={5000}
                value={qty || ""}
                onChange={(e) => setQty(Number(e.target.value))}
                className="rounded-lg border border-line bg-white px-2 py-1.5 font-mono text-sm text-ink outline-none focus:border-steel"
                required
              />
            </label>
            <label className="flex flex-col gap-0.5 text-xs text-ink-faint">
              交期
              <input
                type="date"
                min={plan.plan_start}
                value={due}
                onChange={(e) => setDue(e.target.value)}
                className="rounded-lg border border-line bg-white px-2 py-1.5 font-mono text-sm text-ink outline-none focus:border-steel"
                required
              />
            </label>
            <label className="flex flex-col gap-0.5 text-xs text-ink-faint">
              優先
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as "一般" | "急件")}
                className="rounded-lg border border-line bg-white px-2 py-1.5 text-sm text-ink outline-none focus:border-steel"
              >
                <option value="一般">一般</option>
                <option value="急件">急件</option>
              </select>
            </label>
            <label className="flex flex-col gap-0.5 text-xs text-ink-faint">
              備註
              <input
                value={note}
                maxLength={200}
                onChange={(e) => setNote(e.target.value)}
                placeholder="選填"
                className="rounded-lg border border-line bg-white px-2 py-1.5 text-sm text-ink outline-none focus:border-steel"
              />
            </label>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              disabled={busy || !qty || !due}
              className="rounded-lg bg-steel px-4 py-2 font-bold text-white transition hover:bg-steel-deep disabled:opacity-50"
            >
              {busy ? "開立中…" : "開立工單"}
            </button>
            {created && (
              <span className="text-jade">
                已開立 <b className="font-mono">{created.wo_no}</b>（{created.qty} 件，交期 {fmt(created.due_on)}）·{" "}
                <Link to="/schedule" className="font-bold underline">
                  前往排程
                </Link>
              </span>
            )}
            {failure && <span className="text-seal">{failure}</span>}
          </div>
          <p className="mt-2 text-[11px] text-ink-faint">
            工單寫入生產資料庫，工廠資料庫同步更新（庫存查詢頁的「生產中」、Text-to-SQL 都看得到）；排程時從 {plan.plan_start} 起排。
          </p>
        </form>
      )}
    </section>
  );
}
