import { useMemo, useState } from "react";
import type { AxisDay, ScheduledOp } from "../../api/client";

/** 甘特圖用到的工單欄位（排程中的工單或已排好的工單都有） */
export interface GanttOrder {
  wo_no: string;
  part_name: string;
  priority: string;
  qty: number;
  due_min: number;
  late_min?: number | null;
}

export interface GanttMachine {
  machine_id: string;
  name: string;
  machine_type: string;
}

// 工單配色：深淺都看得清楚、彼此可區分
export const ORDER_COLORS = [
  "#2b5d8a",
  "#b3261e",
  "#2f6b57",
  "#9a5b0b",
  "#6b4fa0",
  "#0f7c8c",
  "#a0457a",
  "#5b6b2f",
  "#c0612b",
  "#3d4f7a",
  "#7a5c3d",
  "#1f7a4d",
];

export const orderColor = (orders: GanttOrder[], woNo: string) => {
  const i = orders.findIndex((o) => o.wo_no === woNo);
  return ORDER_COLORS[(i < 0 ? 0 : i) % ORDER_COLORS.length];
};

const LABEL_W = 132;
const ROW_H = 30;
const HEAD_H = 34;

const hm = (min: number) => (min >= 60 ? `${Math.floor(min / 60)} 小時 ${min % 60 ? `${min % 60} 分` : ""}` : `${min} 分`);

/**
 * 甘特圖：橫軸是「工作時間」（每個工作日一格，週末與假日不佔位置），
 * 機台檢視一列一台機台；工單檢視一列一張工單（含委外與交期線）。
 */
export function GanttChart({
  machines,
  operations,
  orders,
  axis,
  dayMinutes,
  view,
}: {
  machines: GanttMachine[];
  operations: ScheduledOp[];
  orders: GanttOrder[];
  axis: AxisDay[];
  dayMinutes: number;
  view: "machine" | "order";
}) {
  const [hover, setHover] = useState<string | null>(null);
  const end = Math.max(dayMinutes, ...operations.map((o) => o.end_min), ...orders.map((o) => o.due_min));
  const days = Math.min(axis.length, Math.ceil(end / dayMinutes) + 1);
  const dayW = days > 16 ? 64 : 84;
  const width = LABEL_W + days * dayW;
  const x = (min: number) => LABEL_W + (min / dayMinutes) * dayW;

  const rows = useMemo(() => {
    if (view === "machine") {
      return machines.map((m) => ({
        key: m.machine_id,
        label: m.machine_id,
        sub: `${m.machine_type}・${m.name}`,
        ops: operations.filter((o) => o.machine_id === m.machine_id),
        due: null as number | null,
        late: false,
      }));
    }
    return orders.map((w) => ({
      key: w.wo_no,
      label: w.wo_no,
      sub: `${w.part_name}${w.priority === "急件" ? "・急件" : ""}`,
      ops: operations.filter((o) => o.wo_no === w.wo_no),
      due: w.due_min,
      late: (w.late_min ?? 0) > 0,
    }));
  }, [view, machines, orders, operations]);

  const height = HEAD_H + rows.length * ROW_H + 8;

  return (
    <div className="card overflow-x-auto">
      <svg width={width} height={height} className="block text-[11px]" role="img" aria-label="生產排程甘特圖">
        <defs>
          <pattern id="setup-hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="5" height="5" fill="white" fillOpacity="0.35" />
            <line x1="0" y1="0" x2="0" y2="5" stroke="white" strokeOpacity="0.7" strokeWidth="2" />
          </pattern>
        </defs>

        {/* 日期刻度與假日 */}
        {axis.slice(0, days).map((d, i) => {
          const left = LABEL_W + i * dayW;
          const weekStart = i > 0 && d.weekday === "一";
          return (
            <g key={d.date}>
              <rect x={left} y={HEAD_H} width={dayW} height={height - HEAD_H} fill={i % 2 ? "#00000006" : "transparent"} />
              <line
                x1={left}
                x2={left}
                y1={HEAD_H - 6}
                y2={height}
                stroke={d.holidays_before.length || weekStart ? "#7a7a7a" : "#e0e0e0"}
                strokeWidth={weekStart || d.holidays_before.length ? 1.5 : 1}
                strokeDasharray={d.holidays_before.length ? "3 3" : undefined}
              />
              <text x={left + 4} y={14} className="fill-ink-80 font-mono" fontSize="11">
                {d.date.slice(5).replace("-", "/")}
              </text>
              <text x={left + 4} y={27} className="fill-ink-48" fontSize="10">
                {d.weekday}
                {d.holidays_before.length > 0 &&
                  ` ・${d.holidays_before.map((h) => `${String(h.date).slice(5).replace("-", "/")}${h.name}`).join("、")}`}
              </text>
            </g>
          );
        })}

        {rows.map((r, ri) => {
          const y = HEAD_H + ri * ROW_H;
          return (
            <g key={r.key}>
              <line x1={0} x2={width} y1={y + ROW_H} y2={y + ROW_H} stroke="#e0e0e0" />
              <text x={8} y={y + 13} className="fill-ink font-mono" fontSize="11" fontWeight={600}>
                {r.label}
              </text>
              <text x={8} y={y + 25} className="fill-ink-48" fontSize="10">
                {r.sub.length > 12 ? `${r.sub.slice(0, 12)}…` : r.sub}
              </text>

              {r.ops.map((o) => {
                const left = x(o.start_min);
                const w = Math.max(3, x(o.end_min) - left);
                const color = orderColor(orders, o.wo_no);
                const dim = hover !== null && hover !== o.wo_no;
                const outsourced = o.kind === "委外";
                const setupW = o.setup_min ? Math.min(w, (o.setup_min / dayMinutes) * dayW) : 0;
                const label = view === "machine" ? `${o.wo_no.slice(3)}・${o.op_seq}` : outsourced ? o.op_name : (o.machine_id ?? "");
                return (
                  <g
                    key={o.op_id}
                    opacity={dim ? 0.25 : 1}
                    onMouseEnter={() => setHover(o.wo_no)}
                    onMouseLeave={() => setHover(null)}
                    style={{ transition: "opacity 120ms" }}
                  >
                    <title>
                      {`${o.wo_no} 工序 ${o.op_seq}：${o.op_name}\n${o.machine_id ?? "委外"}　${o.start_at} → ${o.end_at}` +
                        (o.setup_min ? `\n換線準備 ${hm(o.setup_min)}＋加工 ${hm(o.run_min)}` : `\n${outsourced ? "委外" : "加工"} ${hm(o.run_min)}`) +
                        (o.pinned ? "\n生產中（釘選，不會被移動）" : "")}
                    </title>
                    <rect
                      x={left}
                      y={y + 5}
                      width={w}
                      height={ROW_H - 10}
                      rx={4}
                      fill={outsourced ? "white" : color}
                      stroke={color}
                      strokeWidth={outsourced ? 1.5 : o.pinned ? 2.5 : 0}
                      strokeDasharray={outsourced ? "4 3" : undefined}
                    />
                    {setupW > 2 && !outsourced && (
                      <rect x={left} y={y + 5} width={setupW} height={ROW_H - 10} rx={4} fill="url(#setup-hatch)" />
                    )}
                    {o.pinned && (
                      <text x={left + 3} y={y + 19} fontSize="11" fill="white">
                        📌
                      </text>
                    )}
                    {w > 46 && (
                      <text
                        x={left + (o.pinned ? 18 : 5)}
                        y={y + 19}
                        fontSize="10.5"
                        fill={outsourced ? color : "white"}
                        fontWeight={600}
                        className="pointer-events-none"
                      >
                        {label.length * 7 > w - 10 ? label.slice(0, Math.max(1, Math.floor((w - 10) / 7))) : label}
                      </text>
                    )}
                  </g>
                );
              })}

              {r.due !== null && (
                <g>
                  <line
                    x1={x(r.due)}
                    x2={x(r.due)}
                    y1={y + 2}
                    y2={y + ROW_H - 2}
                    stroke={r.late ? "#d70015" : "#248a3d"}
                    strokeWidth={2.5}
                  />
                  <title>交期（當天下班）</title>
                </g>
              )}
            </g>
          );
        })}
        <line x1={LABEL_W} x2={LABEL_W} y1={0} y2={height} stroke="#e0e0e0" />
      </svg>
    </div>
  );
}

export function GanttLegend({ view }: { view: "machine" | "order" }) {
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-48">
      <span className="inline-flex items-center gap-1">
        <span className="inline-block h-3 w-5 rounded-sm" style={{ backgroundColor: ORDER_COLORS[0] }} /> 加工
      </span>
      <span className="inline-flex items-center gap-1">
        <span
          className="inline-block h-3 w-5 rounded-sm"
          style={{
            backgroundColor: ORDER_COLORS[0],
            backgroundImage: "repeating-linear-gradient(45deg, #ffffff99 0 2px, transparent 2px 5px)",
          }}
        />
        換線準備
      </span>
      <span className="inline-flex items-center gap-1">📌 生產中（釘選）</span>
      {view === "order" && (
        <>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-3 w-5 rounded-sm border border-dashed" style={{ borderColor: ORDER_COLORS[0] }} /> 委外
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-3 w-0.5 bg-success" /> 交期（準時）
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-3 w-0.5 bg-danger" /> 交期（延遲）
          </span>
        </>
      )}
      <span>橫軸只算上班時間（每天 8 小時），週末與假日不佔位置；滑鼠移到色塊看細節</span>
    </p>
  );
}
