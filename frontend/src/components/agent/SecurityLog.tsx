import type { SecurityLogsResponse } from "../../api/client";
import { useSecurityLogs } from "../../api/hooks";
import { formatTaipei } from "../../lib/format";

const STAGE_LABEL: Record<number, string> = { 1: "認證與授權", 2: "Jev Choice", 4: "Jev Noul" };

/** 今天擋下與剔除的紀錄（系統狀態頁用；只有 access.yaml 的 views.security_logs 看得到） */
export function SecurityLogList({ data, compact = false }: { data: SecurityLogsResponse; compact?: boolean }) {
  const t = data.today;
  return (
    <div className="text-xs">
      <p className="text-ink-80">
        今天：認證與授權擋下 <b>{t.rbac ?? 0}</b> 筆・Jev Choice 擋下 <b>{t.guard ?? 0}</b> 筆・Jev Noul 剔除洩密段落{" "}
        <b>{t.post ?? 0}</b> 段（只存遮蔽個資後的文字）
      </p>
      {data.items.length > 0 ? (
        <ul className="mt-2 flex flex-col divide-y divide-hairline rounded-lg border border-hairline bg-card">
          {data.items.slice(0, compact ? 6 : 20).map((x) => (
            <li key={x.no} className="flex flex-wrap items-baseline gap-x-2 px-2.5 py-1.5">
              <span className="font-mono text-ink-48">{x.no}</span>
              <span
                className={`rounded px-1.5 py-0.5 font-semibold ${x.stage === 4 ? "bg-warning-soft text-warning" : "bg-danger-soft text-danger"}`}
              >
                {STAGE_LABEL[x.stage]}・{x.rule}
              </span>
              <span className="text-ink-48">
                {formatTaipei(x.created_at)}・{x.account_label ?? "—"}・{x.judge}
              </span>
              <span className="w-full truncate text-ink-80" title={x.text ?? ""}>
                {x.text}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-ink-48">還沒有擋下或剔除的紀錄。</p>
      )}
    </div>
  );
}

export function SecurityLogPanel({ compact = false }: { compact?: boolean }) {
  const { data } = useSecurityLogs();
  if (!data) return null;
  return <SecurityLogList data={data} compact={compact} />;
}
