import { useState } from "react";
import type { RouteResponse, SecurityLogsResponse } from "../../api/client";
import { useSecurityLogs, useSwitchAccount } from "../../api/hooks";
import { formatTaipei } from "../../lib/format";

const STAGE_LABEL: Record<number, string> = { 1: "RBAC", 2: "Jev 護欄", 4: "Jev 過濾" };

/**
 * 第 1 段（RBAC）或第 2 段（Jev 護欄）擋下的請求：哪一段、每項檢查、紀錄編號、誰判斷的。
 * 只是身分不對（RBAC）時給「切換成〇〇再試一次」：身分只看伺服器端工作階段，打字自稱沒有用。
 */
export function BlockedCard({ route, onRetry }: { route: RouteResponse; onRetry: () => void }) {
  const switchAccount = useSwitchAccount();
  const [busy, setBusy] = useState(false);
  const b = route.blocked!;
  const checks = b.stage === 1 ? route.rbac.checks : (route.guard?.checks ?? []);
  const retry = b.stage === 1 ? route.rbac.retry : null;

  const retryAs = async () => {
    if (!retry) return;
    setBusy(true);
    try {
      await switchAccount(retry.account_id);
      onRetry();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-seal/30 bg-seal-soft/50 p-3 text-sm">
      <p className="font-bold text-seal-deep">
        {b.stage === 1 ? "RBAC 權限檢查沒有通過" : `Jev 第一層護欄擋下了這個請求（${b.rule}）`}
      </p>
      <p className="mt-0.5 text-ink-soft">
        {b.stage === 1
          ? "這個請求沒有送給 Jev，也沒有碰到任何資料。"
          : "它沒有進入檢索，也沒有碰到任何資料與模型。"}
      </p>
      <ul className="mt-2 flex flex-col gap-1 text-xs">
        {checks
          .filter((c) => c.ok === false)
          .map((c) => (
            <li key={c.key} className="flex gap-2">
              <span className="w-16 shrink-0 font-bold text-seal">{c.label}</span>
              <span className="text-ink-soft">
                <span className="text-ink-faint">{c.by}・</span>
                {c.detail}
              </span>
            </li>
          ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint">
        <span>
          已拒絕並記錄 <b className="font-mono text-ink-soft">{b.log_no}</b>
        </span>
        <span>判斷：{b.judge}</span>
        {retry && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void retryAs()}
            className="ml-auto rounded-lg bg-ink px-3 py-1.5 font-bold text-paper transition hover:bg-ink/85 disabled:opacity-50"
          >
            {busy ? "切換中…" : `切換成〈${retry.label}〉再試一次`}
          </button>
        )}
      </div>
    </div>
  );
}

/** 今天擋下與移除的紀錄（系統狀態、「最近擋下了哪些請求？」都用這個） */
export function SecurityLogList({ data, compact = false }: { data: SecurityLogsResponse; compact?: boolean }) {
  const t = data.today;
  return (
    <div className="text-xs">
      <p className="text-ink-soft">
        今天：RBAC 擋下 <b>{t.rbac ?? 0}</b> 筆・Jev 護欄擋下 <b>{t.guard ?? 0}</b> 筆・Jev 過濾移除段落{" "}
        <b>{t.post ?? 0}</b> 段（只存遮蔽個資後的文字）
      </p>
      {data.items.length > 0 ? (
        <ul className="mt-2 flex flex-col divide-y divide-line rounded-lg border border-line bg-card">
          {data.items.slice(0, compact ? 6 : 20).map((x) => (
            <li key={x.no} className="flex flex-wrap items-baseline gap-x-2 px-2.5 py-1.5">
              <span className="font-mono text-ink-faint">{x.no}</span>
              <span
                className={`rounded px-1.5 py-0.5 font-bold ${x.stage === 4 ? "bg-amber-soft text-amber" : "bg-seal-soft text-seal-deep"}`}
              >
                {STAGE_LABEL[x.stage]}・{x.rule}
              </span>
              <span className="text-ink-faint">
                {formatTaipei(x.created_at)}・{x.account_label ?? "—"}・{x.judge}
              </span>
              <span className="w-full truncate text-ink-soft" title={x.text ?? ""}>
                {x.text}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-ink-faint">還沒有擋下或移除的紀錄。</p>
      )}
    </div>
  );
}

export function SecurityLogPanel({ compact = false }: { compact?: boolean }) {
  const { data } = useSecurityLogs();
  if (!data) return null;
  return <SecurityLogList data={data} compact={compact} />;
}
