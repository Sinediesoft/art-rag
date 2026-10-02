import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ApiError,
  type Approval,
  type ChangeCheck,
  type ChangeCommitted,
  type ChangeDiff,
  type ChangePreview,
  type ChangePreviewRequest,
} from "../../api/client";

const SOURCE_STYLE: Record<string, string> = {
  規則: "bg-paper-deep text-ink-soft",
  推定: "bg-amber-soft text-amber",
  "Qwen3-VL": "bg-steel-soft text-steel-deep",
  表單: "bg-paper-deep text-ink-soft",
};

const val = (v: ChangeDiff["before"]) => (v === null || v === undefined ? "（無）" : String(v));

/** 寫入後要重新抓的資料：庫存、工單、排程、核准 */
const STALE = ["inventory-overview", "part-inventory", "part-plan", "production-overview", "approvals", "accounts", "audit"];

export function CheckList({ checks }: { checks: ChangeCheck[] }) {
  return (
    <ol className="grid gap-1 sm:grid-cols-2">
      {checks.map((c) => (
        <li
          key={c.key}
          className={`flex items-start gap-2 rounded-lg px-2 py-1.5 ${
            c.ok === false ? "bg-seal-soft/70" : c.ok ? "bg-jade-soft/60" : "bg-paper-deep/60"
          }`}
        >
          <span
            className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full text-[10px] font-black text-white ${
              c.ok === false ? "bg-seal" : c.ok ? "bg-jade" : "bg-ink-faint/50"
            }`}
          >
            {c.ok === false ? "✕" : c.ok ? "✓" : "–"}
          </span>
          <span>
            <b className="text-ink">{c.label}</b> <span className="text-ink-soft">{c.detail}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

export function DiffTable({ rows }: { rows: ChangeDiff[] }) {
  if (!rows.length) return null;
  return (
    <table className="w-full text-left text-xs">
      <thead className="text-ink-faint">
        <tr>
          <th className="py-1 font-medium">資料</th>
          <th className="py-1 font-medium">欄位</th>
          <th className="py-1 text-right font-medium">修改前</th>
          <th className="py-1 text-right font-medium">修改後</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-t border-line">
            <td className="py-1 pr-2">{r.label}</td>
            <td className="py-1 pr-2 text-ink-soft">{r.field}</td>
            <td className="py-1 text-right font-mono text-ink-faint">{val(r.before)}</td>
            <td className="py-1 text-right font-mono font-bold">{val(r.after)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * 修改資料流程：參數抽取 → 權限判定（角色、範圍、欄位、上限）→ 試算 → 額度判斷 →
 * 確認卡（額度內）或送主管核准（超額）→ 依資料庫讀回結果回覆。
 */
export function ChangeCard({ request }: { request: ChangePreviewRequest }) {
  const qc = useQueryClient();
  const [preview, setPreview] = useState<ChangePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ChangeCommitted | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [note, setNote] = useState("");
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // StrictMode 會跑兩次 effect：試算只做一次
    started.current = true;
    api.changePreview(request).then(setPreview, (e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [request]);

  const refresh = () => STALE.forEach((key) => void qc.invalidateQueries({ queryKey: [key] }));

  const commit = async () => {
    if (!preview?.pending_id) return;
    setBusy(true);
    setError(null);
    try {
      setDone(await api.changeCommit(preview.pending_id));
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message}（${e.code}）` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const submitApproval = async () => {
    if (!preview?.pending_id) return;
    setBusy(true);
    setError(null);
    try {
      setApproval(await api.requestApproval(preview.pending_id, note.trim()));
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message}（${e.code}）` : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!preview)
    return error ? (
      <p className="text-sm text-seal">{error}</p>
    ) : (
      <p className="flex items-center gap-2 text-sm text-ink-soft">
        <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-seal" />
        抽取參數、權限判定、試算中…
      </p>
    );

  const p = preview;
  const finished = done || approval;
  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-seal-soft px-2 py-0.5 text-xs font-bold text-seal-deep">修改資料 · {p.op_label}</span>
        <span className="text-xs text-ink-faint">
          以「{p.account.label}」身分 · {p.latency_ms ?? 0} ms
        </span>
      </div>

      {Object.keys(p.param_labels).length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          {Object.entries(p.param_labels).map(([k, v]) => (
            <span key={k} className="rounded-lg bg-card px-2 py-1 ring-1 ring-line">
              <span className="text-ink-faint">{k}</span> <b>{String(v)}</b>
            </span>
          ))}
        </div>
      )}
      {Object.keys(p.sources).length > 0 && (
        <p className="flex flex-wrap items-center gap-1 text-[11px] text-ink-faint">
          參數來源：
          {[...new Set(Object.values(p.sources))].map((s) => (
            <span key={s} className={`rounded px-1.5 py-0.5 ${SOURCE_STYLE[s] ?? ""}`}>
              {s}
            </span>
          ))}
          {p.llm && <span>（Qwen3-VL {String((p.llm as { ms?: number }).ms ?? "")} ms）</span>}
          {p.notes.map((n) => (
            <span key={n} className="text-amber">
              · {n}
            </span>
          ))}
        </p>
      )}

      {p.checks.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-bold tracking-wide text-ink-faint">權限判定（預設不允許，依序檢查）</p>
          <CheckList checks={p.checks} />
        </div>
      )}

      {p.diff.length > 0 && (
        <div className="rounded-lg border border-line bg-card px-3 py-2">
          <p className="mb-1 text-xs font-bold tracking-wide text-ink-faint">試算結果（交易內套用後已回滾，尚未寫入）</p>
          <DiffTable rows={p.diff} />
        </div>
      )}

      {p.next === "rejected" && (
        <div className="rounded-lg border border-seal/30 bg-seal-soft/60 p-3">
          <p className="font-bold text-seal-deep">已拒絕，沒有任何資料被修改</p>
          <p className="text-ink-soft">{p.message}</p>
          <p className="mt-1 text-[11px] text-ink-faint">這次嘗試已記進稽核紀錄。</p>
        </div>
      )}
      {p.next === "need_info" && (
        <div className="rounded-lg border border-amber/30 bg-amber-soft/60 p-3 text-amber">{p.message}</div>
      )}

      {p.next === "confirm" && !finished && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-jade/30 bg-jade-soft/40 p-3">
          <span className="text-jade">{p.message}</span>
          <button
            onClick={() => void commit()}
            disabled={busy}
            className="ml-auto rounded-lg bg-jade px-4 py-2 font-bold text-white transition hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "寫入中…" : "確認寫入"}
          </button>
        </div>
      )}

      {p.next === "approval" && !finished && (
        <div className="flex flex-col gap-2 rounded-lg border border-amber/40 bg-amber-soft/50 p-3">
          <p className="font-bold text-amber">超過額度，需要主管核准</p>
          <ul className="list-disc pl-5 text-ink-soft">
            {p.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={200}
              placeholder="申請說明（選填，例如原因）"
              className="min-w-0 flex-1 rounded-lg border border-line bg-white px-3 py-2 text-sm outline-none focus:border-amber"
            />
            <button
              onClick={() => void submitApproval()}
              disabled={busy}
              className="rounded-lg bg-amber px-4 py-2 font-bold text-white transition hover:opacity-90 disabled:opacity-50"
            >
              {busy ? "送出中…" : "送主管核准"}
            </button>
          </div>
        </div>
      )}

      {done && (
        <div className="rounded-lg border border-jade/30 bg-jade-soft/60 p-3">
          <p className="font-bold text-jade">✓ {done.text}</p>
          <p className="mt-1 text-[11px] text-ink-faint">
            回覆依資料庫讀回的實際結果產生；異動單與稽核紀錄已寫入，工廠資料庫已同步（Text-to-SQL 查得到）。
          </p>
        </div>
      )}
      {approval && (
        <div className="rounded-lg border border-amber/30 bg-amber-soft/60 p-3">
          <p className="font-bold text-amber">已建立待核准單 {approval.ap_no}，等主管核准</p>
          <p className="text-ink-soft">
            切換成「主管」到{" "}
            <Link to="/approvals" className="font-bold underline">
              待核准清單
            </Link>{" "}
            處理；核准時會重新試算，資料若已變動申請會失效。
          </p>
        </div>
      )}
      {error && <p className="rounded-lg bg-seal-soft/60 p-2 text-seal">{error}</p>}
    </div>
  );
}
