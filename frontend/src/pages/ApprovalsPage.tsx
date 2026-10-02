import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Approval, type ApprovalDecision } from "../api/client";
import { useAccounts, useApprovals, useAudit } from "../api/hooks";
import { DiffTable } from "../components/agent/ChangeCard";
import { ErrorMessage, Loading } from "../components/common/Feedback";

const STATUS_STYLE: Record<string, string> = {
  待核准: "bg-amber-soft text-amber",
  已核准: "bg-jade-soft text-jade",
  已退回: "bg-seal-soft text-seal-deep",
  已失效: "bg-paper-deep text-ink-faint",
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
      <section className="rounded-2xl border border-line bg-card p-5 shadow-sm sm:p-8">
        <p className="text-sm font-bold tracking-widest text-seal">修改資料 · 主管核准</p>
        <h1 className="mt-1 text-3xl font-black">待核准清單</h1>
        <p className="mt-2 max-w-2xl text-ink-soft">
          超過額度的修改（報廢超過 10 件、盤點差異超過 20 件或 20%、交期延後超過 7 天、急件工單…）會在這裡等主管處理。
          核准前會重新試算；申請後資料若被改過，申請自動失效，請申請人重送。超過 24 小時沒處理也會失效。
        </p>
        {me && !data.can_approve && (
          <p className="mt-3 inline-block rounded-lg bg-amber-soft px-3 py-1.5 text-sm text-amber">
            目前身分「{me.label}」不能核准，請在頁首切換成「主管」。
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-bold">待核准（{data.pending.length}）</h2>
        {data.pending.length === 0 && <p className="text-sm text-ink-faint">目前沒有待核准的申請。</p>}
        {data.pending.map((a) => (
          <ApprovalCard key={a.ap_no} ap={a} canApprove={data.can_approve} myId={me?.id} />
        ))}
      </section>

      {data.mine.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-lg font-bold">我的申請</h2>
          <ApprovalTable rows={data.mine} />
        </section>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-bold">最近的核准紀錄</h2>
        {data.recent.length ? <ApprovalTable rows={data.recent} /> : <p className="text-sm text-ink-faint">尚無紀錄。</p>}
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
    <article className="rounded-xl border border-amber/30 bg-card p-4 text-sm shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b className="font-mono">{ap.ap_no}</b>
        <span className="rounded-full bg-seal-soft px-2 py-0.5 text-xs font-bold text-seal-deep">{ap.op_label}</span>
        <span className="text-xs text-ink-faint">
          {ap.requester_label} 申請 · {time(ap.created_at)}
        </span>
      </div>
      <p className="mt-1 font-bold">{ap.summary}</p>
      <ul className="mt-1 list-disc pl-5 text-amber">
        {ap.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {ap.note && <p className="mt-1 text-ink-soft">申請說明：{ap.note}</p>}
      <div className="mt-2 rounded-lg border border-line px-3 py-2">
        <p className="mb-1 text-xs text-ink-faint">申請時的試算結果</p>
        <DiffTable rows={ap.diff} />
      </div>
      {result ? (
        <p className={`mt-3 rounded-lg p-2 font-bold ${result.status === "已核准" ? "bg-jade-soft text-jade" : "bg-paper-deep text-ink-soft"}`}>
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
            className="min-w-0 flex-1 rounded-lg border border-line bg-white px-3 py-2 outline-none focus:border-steel disabled:bg-paper"
          />
          <button
            onClick={() => void act("approve")}
            disabled={busy || !canApprove || own}
            className="rounded-lg bg-jade px-4 py-2 font-bold text-white transition hover:opacity-90 disabled:opacity-40"
          >
            核准
          </button>
          <button
            onClick={() => void act("return")}
            disabled={busy || !canApprove || own}
            className="rounded-lg border border-seal px-4 py-2 font-bold text-seal transition hover:bg-seal-soft disabled:opacity-40"
          >
            退回
          </button>
        </div>
      )}
      {own && !result && <p className="mt-1 text-xs text-ink-faint">這是你自己的申請，不能自己核准。</p>}
      {error && <p className="mt-2 text-seal">{error}</p>}
    </article>
  );
}

function ApprovalTable({ rows }: { rows: Approval[] }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-card">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="bg-paper-deep/60 text-xs text-ink-faint">
          <tr>
            <th className="px-3 py-2 font-medium">單號</th>
            <th className="px-3 py-2 font-medium">內容</th>
            <th className="px-3 py-2 font-medium">申請人</th>
            <th className="px-3 py-2 font-medium">狀態</th>
            <th className="px-3 py-2 font-medium">處理</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.ap_no} className="border-t border-line align-top">
              <td className="px-3 py-2 font-mono text-xs">{a.ap_no}</td>
              <td className="px-3 py-2">{a.summary}</td>
              <td className="px-3 py-2 text-ink-soft">{a.requester_label}</td>
              <td className="px-3 py-2">
                <span className={`rounded-full px-2 py-0.5 text-xs font-bold ${STATUS_STYLE[a.status] ?? ""}`}>{a.status}</span>
              </td>
              <td className="px-3 py-2 text-xs text-ink-soft">
                {a.decided_label && `${a.decided_label} · ${time(a.decided_at)}`}
                {a.change_no && <span className="ml-1 font-mono text-jade">→ {a.change_no}</span>}
                {a.decision_note && <div className="text-ink-faint">{a.decision_note}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ACTION_STYLE: Record<string, string> = {
  寫入: "text-jade",
  核准寫入: "text-jade",
  送核准: "text-amber",
  退回: "text-seal",
  拒絕: "text-seal",
  拒絕寫入: "text-seal",
  拒絕核准: "text-seal",
  寫入失敗: "text-seal",
  失效: "text-ink-faint",
};

function AuditSection() {
  const { data } = useAudit();
  if (!data) return null;
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-bold">稽核紀錄</h2>
        <Link to="/inventory" className="text-xs font-bold text-steel hover:underline">
          用 Text-to-SQL 查異動 →
        </Link>
      </div>
      <p className="text-xs text-ink-faint">寫入、拒絕、送核准、核准、退回、失效都記；make demo-reset 會一併清掉。</p>
      <div className="overflow-x-auto rounded-xl border border-line bg-card">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="bg-paper-deep/60 text-xs text-ink-faint">
            <tr>
              <th className="px-3 py-2 font-medium">時間</th>
              <th className="px-3 py-2 font-medium">身分</th>
              <th className="px-3 py-2 font-medium">動作</th>
              <th className="px-3 py-2 font-medium">單號</th>
              <th className="px-3 py-2 font-medium">內容</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((a) => (
              <tr key={a.id} className="border-t border-line align-top">
                <td className="whitespace-nowrap px-3 py-1.5 text-xs text-ink-faint">{time(a.at)}</td>
                <td className="whitespace-nowrap px-3 py-1.5">{a.actor_label}</td>
                <td className={`whitespace-nowrap px-3 py-1.5 font-bold ${ACTION_STYLE[a.action] ?? ""}`}>{a.action}</td>
                <td className="whitespace-nowrap px-3 py-1.5 font-mono text-xs">{a.ref_no}</td>
                <td className="px-3 py-1.5 text-ink-soft">{a.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
