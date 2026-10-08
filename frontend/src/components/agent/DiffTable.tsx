import type { ChangeDiff } from "../../api/client";

const val = (v: ChangeDiff["before"]) => (v === null || v === undefined ? "（無）" : String(v));

/** 修改前後對照（主管核准頁用；對話裡的試算卡在 components/shell/Message.tsx） */
export function DiffTable({ rows }: { rows: ChangeDiff[] }) {
  if (!rows.length) return null;
  return (
    <table className="w-full text-left text-xs">
      <thead className="text-ink-48">
        <tr>
          <th className="py-1 font-normal">資料</th>
          <th className="py-1 font-normal">欄位</th>
          <th className="py-1 text-right font-normal">修改前</th>
          <th className="py-1 text-right font-normal">修改後</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-t border-hairline">
            <td className="py-1 pr-2">{r.label}</td>
            <td className="py-1 pr-2 text-ink-80">{r.field}</td>
            <td className="py-1 text-right font-mono text-ink-48">{val(r.before)}</td>
            <td className="py-1 text-right font-mono font-semibold">{val(r.after)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
