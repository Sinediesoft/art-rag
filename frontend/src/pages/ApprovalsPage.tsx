import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Approval, type ApprovalDecision } from "../api/client";
import { useAccounts, useApprovals, useAudit } from "../api/hooks";
import { DiffTable } from "../components/agent/DiffTable";
import { ErrorMessage, Loading } from "../components/common/Feedback";
import { RegionDraftsSection } from "../components/regions/RegionDrafts";

const STATUS_STYLE: Record<string, string> = {
  待核准: "bg-warning-soft text-warning",
  已核准: "bg-success-soft text-success",
  已退回: "bg-danger-soft text-danger",
  已失效: "bg-parchment-deep text-ink-48",
};

const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";

/**
 * 主管核准：待核准清單只能在這裡按按鈕處理，不接受用對話核准（避免在聊天裡誘導系統核准）。
 * 核准時重新試算、比對申請時的資料；寫入前再驗權限與資料版本，任何一項變了申請就失效。
 */
export function ApprovalsPage() {
  const { data, isLoading, error } = useApprovals();
  const { data: accounts } = useAccounts();
  if (isLoading) return <Loading />;
  if (error || !data) return <ErrorMessage message={(error as Error)?.message ?? ""} />;
  const me = accounts?.current;

  return (
    <div className="flex flex-col gap-8">
      <section className="card p-6 sm:p-10">
        <p className="t-eyebrow">修改資料 · 主管核准</p>
        <h1 className="t-display mt-2">待核准清單</h1>
        <p className="mt-3 max-w-2xl text-ink-80">
          超過額度的修改（報廢超過 10 件、盤點差異超過 20 件或 20%、交期延後超過 7 天、急件工單…）會在這裡等主管處理。
          核准前會重新試算；申請後資料若被改過，申請自動失效，請申請人重送。超過 24 小時沒處理也會失效。
        </p>
        {me && !data.can_approve && (
          <p className="mt-3 inline-block rounded-lg bg-warning-soft px-3 py-1.5 text-sm text-warning">
            目前身分「{me.label}」不能核准，請在頁首切換成「主管」。
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">待核准（{data.pending.length}）</h2>
        {data.pending.length === 0 && <p className="text-sm text-ink-48">目前沒有待核准的申請。</p>}
        {data.pending.map((a) => (
          <ApprovalCard key={a.ap_no} ap={a} canApprove={data.can_approve} myId={me?.id} />
        ))}
      </section>

      <RegionDraftsSection />

      {data.mine.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-lg font-semibold">我的申請</h2>
          <ApprovalTable rows={data.mine} />
        </section>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-semibold">最近的核准紀錄</h2>
        {data.recent.length ? <ApprovalTable rows={data.recent} /> : <p className="text-sm text-ink-48">尚無紀錄。</p>}
      </section>

      <AuditSection />
    </div>
  );
}

function ApprovalCard({ ap, canApprove, myId }: { ap: Approval; canApprove: boolean; myId?: string }) {
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const own = ap.requester_id === myId;

  const act = async (kind: "approve" | "return") => {
    if (kind === "return" && !note.trim()) {
      setError("退回要附理由（寫在上面的說明欄）");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = kind === "approve" ? await api.approve(ap.ap_no, note.trim()) : await api.returnApproval(ap.ap_no, note.trim());
      setResult(r);
      for (const key of ["approvals", "accounts", "audit", "inventory-overview", "part-inventory", "part-plan", "production-overview"]) {
        void qc.invalidateQueries({ queryKey: [key] });
      }
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message}（${e.code}）` : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="card border-warning/40 p-5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b className="font-mono">{ap.ap_no}</b>
        <span className="rounded-full bg-parchment-deep px-2 py-0.5 text-xs font-semibold text-ink-80">{ap.op_label}</span>
        <span className="text-xs text-ink-48">
          {ap.requester_label} 申請 · {time(ap.created_at)}
        </span>
      </div>
      <p className="mt-1 font-semibold">{ap.summary}</p>
      <ul className="mt-1 list-disc pl-5 text-warning">
        {ap.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {ap.note && <p className="mt-1 text-ink-80">申請說明：{ap.note}</p>}
      <div className="mt-2 rounded-lg border border-hairline px-3 py-2">
        <p className="mb-1 text-xs text-ink-48">申請時的試算結果</p>
        <DiffTable rows={ap.diff} />
      </div>
      {result ? (
        <p className={`mt-3 rounded-lg p-2 font-semibold ${result.status === "已核准" ? "bg-success-soft text-success" : "bg-parchment-deep text-ink-80"}`}>
          {result.text}
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={200}
            placeholder={canApprove ? "核准備註／退回理由" : "只有主管可以處理"}
            disabled={!canApprove || own}
            className="field min-w-0 flex-1 disabled:bg-parchment"
          />
          <button
            onClick={() => void act("approve")}
            disabled={busy || !canApprove || own}
            className="btn-primary"
          >
            核准
          </button>
          <button
            onClick={() => void act("return")}
            disabled={busy || !canApprove || own}
            className="rounded-full border border-danger/40 px-4 py-2 text-danger transition hover:bg-danger-soft disabled:opacity-40"
          >
            退回
          </button>
        </div>
      )}
      {own && !result && <p className="mt-1 text-xs text-ink-48">這是你自己的申請，不能自己核准。</p>}
      {error && <p className="mt-2 text-danger">{error}</p>}
    </article>
  );
}

function ApprovalTable({ rows }: { rows: Approval[] }) {
  return (
    <div className="card overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="t-caption-strong bg-parchment text-ink-48">
          <tr>
            <th className="px-3 py-2 font-semibold">單號</th>
            <th className="px-3 py-2 font-semibold">內容</th>
            <th className="px-3 py-2 font-semibold">申請人</th>
            <th className="px-3 py-2 font-semibold">狀態</th>
            <th className="px-3 py-2 font-semibold">處理</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.ap_no} className="border-t border-hairline align-top">
              <td className="px-3 py-2 font-mono text-xs">{a.ap_no}</td>
              <td className="px-3 py-2">{a.summary}</td>
              <td className="px-3 py-2 text-ink-80">{a.requester_label}</td>
              <td className="px-3 py-2">
                <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${STATUS_STYLE[a.status] ?? ""}`}>{a.status}</span>
              </td>
              <td className="px-3 py-2 text-xs text-ink-80">
                {a.decided_label && `${a.decided_label} · ${time(a.decided_at)}`}
                {a.change_no && <span className="ml-1 font-mono text-success">→ {a.change_no}</span>}
                {a.decision_note && <div className="text-ink-48">{a.decision_note}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ACTION_STYLE: Record<string, string> = {
  寫入: "text-success",
  核准寫入: "text-success",
  送核准: "text-warning",
  退回: "text-danger",
  拒絕: "text-danger",
  拒絕寫入: "text-danger",
  拒絕核准: "text-danger",
  寫入失敗: "text-danger",
  失效: "text-ink-48",
};

function AuditSection() {
  const { data } = useAudit();
  if (!data) return null;
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">稽核紀錄</h2>
        <Link to="/inventory" className="link text-xs font-semibold">
          用 Text-to-SQL 查異動 →
        </Link>
      </div>
      <p className="text-xs text-ink-48">寫入、拒絕、送核准、核准、退回、失效都記；make demo-reset 會一併清掉。</p>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="t-caption-strong bg-parchment text-ink-48">
            <tr>
              <th className="px-3 py-2 font-semibold">時間</th>
              <th className="px-3 py-2 font-semibold">身分</th>
              <th className="px-3 py-2 font-semibold">動作</th>
              <th className="px-3 py-2 font-semibold">單號</th>
              <th className="px-3 py-2 font-semibold">內容</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((a) => (
              <tr key={a.id} className="border-t border-hairline align-top">
                <td className="whitespace-nowrap px-3 py-1.5 text-xs text-ink-48">{time(a.at)}</td>
                <td className="whitespace-nowrap px-3 py-1.5">{a.actor_label}</td>
                <td className={`whitespace-nowrap px-3 py-1.5 font-semibold ${ACTION_STYLE[a.action] ?? ""}`}>{a.action}</td>
                <td className="whitespace-nowrap px-3 py-1.5 font-mono text-xs">{a.ref_no}</td>
                <td className="px-3 py-1.5 text-ink-80">{a.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
