import { Link } from "react-router-dom";
import { usePartInventory } from "../../api/hooks";

/** 圖紙頁的庫存摘要（固定查詢，不經模型）；「用中文問庫存」帶著品名跳到 Text-to-SQL 頁 */
export function PartInventoryCard({ partId, name }: { partId: string; name: string }) {
  const { data: inv, error } = usePartInventory(partId);
  if (error) return null; // 庫存資料庫沒有這張圖紙（例如剛加入、還沒建庫存資料）
  if (!inv) return null;
  const low = inv.safety_stock != null && inv.available < inv.safety_stock;
  const stats: [string, number, string][] = [
    ["可用", inv.available, low ? "text-danger" : "text-success"],
    ["保留", inv.reserved, "text-ink"],
    ["待檢", inv.inspecting, "text-ink"],
    ["不良", inv.defective, inv.defective ? "text-warning" : "text-ink"],
  ];
  const defects = inv.locations.filter((l) => l.status === "不良" && l.note);

  return (
    <section className="card p-5 text-sm">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">庫存</h2>
        <span className="text-xs text-ink-48">資料日期 {inv.as_of} · 單位：{inv.unit ?? "件"}</span>
      </div>
      <div className="grid grid-cols-4 gap-2 text-center">
        {stats.map(([label, v, color]) => (
          <div key={label} className="rounded-lg bg-parchment py-2.5">
            <p className={`font-display text-xl font-semibold tabular-nums ${color}`}>{v.toLocaleString()}</p>
            <p className="text-xs text-ink-48">{label}</p>
          </div>
        ))}
      </div>
      <p className={`mt-2 text-xs ${low ? "font-semibold text-danger" : "text-ink-48"}`}>
        安全庫存 {inv.safety_stock} · 補貨批量 {inv.reorder_qty} · 前置 {inv.lead_time_days} 天 · {inv.make_or_buy}
        {low && `　⚠ 可用庫存低於安全庫存 ${inv.safety_stock! - inv.available} 件`}
      </p>

      {inv.sales_orders.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-semibold text-ink-48">未出完貨的訂單</p>
          <ul className="flex flex-col gap-0.5">
            {inv.sales_orders.map((o) => (
              <li key={`${o.so_no}-${o.line_no}`} className="flex flex-wrap gap-x-3">
                <span className="font-mono text-xs">{o.so_no}</span>
                <span>{o.customer}</span>
                <span className={o.qty - o.qty_shipped > inv.available ? "font-semibold text-danger" : ""}>
                  未出貨 {o.qty - o.qty_shipped}
                </span>
                <span className="text-ink-48">交期 {o.due_on}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {inv.work_orders.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-xs font-semibold text-ink-48">未完工的工單</p>
          <ul className="flex flex-col gap-0.5">
            {inv.work_orders.map((w) => (
              <li key={w.wo_no} className="flex flex-wrap gap-x-3">
                <span className="font-mono text-xs">{w.wo_no}</span>
                <span>{w.status}</span>
                <span>
                  完工 {w.qty_done}/{w.qty_planned}
                </span>
                <span className="text-ink-48">
                  {w.line} · 預計 {w.due_on}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {defects.length > 0 && (
        <p className="mt-3 text-xs text-warning">
          不良品：{defects.map((d) => `${d.qty} 件（${d.note}）`).join("；")}——對照下方「常見不良與對策」段落
        </p>
      )}

      <Link
        to={`/inventory?q=${encodeURIComponent(`${name}目前在各倉庫各有多少庫存？`)}`}
        className="link mt-3 inline-flex items-center gap-1"
      >
        用中文問庫存（Text-to-SQL）→
      </Link>
    </section>
  );
}
