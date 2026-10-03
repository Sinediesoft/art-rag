import { useState } from "react";
import type { RouteResponse, SecurityLogsResponse } from "../../api/client";
import { useSecurityLogs, useSwitchAccount } from "../../api/hooks";
import { formatTaipei } from "../../lib/format";

const STAGE_LABEL: Record<number, string> = { 1: "認證與授權", 2: "Jev Choice", 4: "Jev Noul" };

/**
 * 第 1 段（認證與授權）或第 2 段（Jev Choice）擋下的請求：哪一段、每項檢查、紀錄編號、誰判斷的。
 * 只是角色不對時給「切換成〇〇再試一次」：身分只看 JWT，打字自稱沒有用。
 * 指定了看不到的圖紙則是降級回應「查無資料」：不說有這份文件、也不建議換誰。
 */
export function BlockedCard({ route, onRetry }: { route: RouteResponse; onRetry: () => void }) {
  const switchAccount = useSwitchAccount();
  const [busy, setBusy] = useState(false);
  const b = route.blocked!;
  const checks = b.stage === 1 ? route.auth.checks : (route.guard?.checks ?? []);
  const retry = b.stage === 1 ? route.auth.retry : null;

  if (b.degraded)
    return (
      <div className="rounded-lg border border-hairline bg-parchment p-3 text-sm">
        <p className="font-semibold text-ink">🔒 {b.reason}</p>
        <p className="mt-0.5 text-xs text-ink-48">
          降級回應：只看你目前憑證權限內的資料，不透露其他文件是否存在。已記錄 <span className="font-mono">{b.log_no}</span>
        </p>
      </div>
    );

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
    <div className="rounded-lg border border-danger/30 bg-danger-soft p-3 text-sm">
      <p className="font-semibold text-danger">
        {b.stage === 1 ? "第 1 段：角色授權沒有通過" : `第 2 段：Jev Choice 擋下了這個請求（${b.rule}）`}
      </p>
      <p className="mt-0.5 text-ink-80">
        {b.stage === 1
          ? "這個請求沒有送給 Jev，也沒有碰到任何資料。"
          : "它沒有進入檢索，也沒有碰到任何資料與模型。"}
      </p>
      <ul className="mt-2 flex flex-col gap-1 text-xs">
        {checks
          .filter((c) => c.ok === false)
          .map((c) => (
            <li key={c.key} className="flex gap-2">
              <span className="w-16 shrink-0 font-semibold text-danger">{c.label}</span>
              <span className="text-ink-80">
                <span className="text-ink-48">{c.by}・</span>
                {c.detail}
              </span>
            </li>
          ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-48">
        <span>
          已拒絕並記錄 <b className="font-mono text-ink-80">{b.log_no}</b>
        </span>
        <span>判斷：{b.judge}</span>
        {retry && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void retryAs()}
            className="btn-dark ml-auto hover:bg-ink-80"
          >
            {busy ? "切換中…" : `切換成〈${retry.label}〉再試一次`}
          </button>
        )}
      </div>
    </div>
  );
}

/** 今天擋下與剔除的紀錄（系統狀態、「最近擋下了哪些請求？」都用這個） */
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
