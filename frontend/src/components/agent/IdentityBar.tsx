import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Account } from "../../api/client";
import { useAccounts } from "../../api/hooks";

const ROLE_STYLE: Record<string, string> = {
  guest: "bg-paper-deep text-ink-soft",
  warehouse: "bg-steel-soft text-steel-deep",
  sales: "bg-amber-soft text-amber",
  planner: "bg-jade-soft text-jade",
  manager: "bg-seal-soft text-seal-deep",
};

/** 這個身分可以改什麼、範圍到哪 */
export function scopeText(a: Account) {
  if (a.role === "guest") return "只能查詢，不能修改";
  if (a.role === "warehouse") return `盤點、調撥、報廢、庫存狀態 · ${a.warehouses.join("、")}`;
  if (a.role === "sales") return `訂單交期、數量 · ${a.customers.join("、")}`;
  if (a.role === "planner") return "開立工單、改交期、取消自己開的工單、執行排程 · 全廠";
  if (a.role === "manager") return "核准超額申請（不能核准自己的）";
  return a.note;
}

/**
 * 展示版身分列：頁首下方切換預設帳號（不用密碼），身分存在伺服器端的工作階段。
 * 同一句話換個身分就會被拒絕——權限由後端判定，模型決定不了。
 */
export function IdentityBar() {
  const qc = useQueryClient();
  const { data } = useAccounts();
  const [busy, setBusy] = useState(false);
  if (!data) return null;
  const me = data.current;

  const change = async (id: string) => {
    setBusy(true);
    try {
      await api.switchAccount(id);
      await qc.invalidateQueries();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="border-b border-line bg-card/80">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5 text-xs">
        <span className="font-bold text-ink-faint">目前身分</span>
        {data.demo_controls ? (
          <select
            value={me.id}
            disabled={busy}
            onChange={(e) => void change(e.target.value)}
            aria-label="切換展示身分"
            className={`rounded-full border-0 py-0.5 pl-2.5 pr-6 text-xs font-bold outline-none ring-1 ring-line focus:ring-steel ${ROLE_STYLE[me.role] ?? ""}`}
          >
            {data.accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
        ) : (
          <span className={`rounded-full px-2.5 py-0.5 font-bold ${ROLE_STYLE[me.role] ?? ""}`}>{me.label}</span>
        )}
        <span className="min-w-0 truncate text-ink-soft">{scopeText(me)}</span>
        <Link
          to="/approvals"
          className={`ml-auto shrink-0 rounded-full px-2.5 py-0.5 font-bold transition ${
            data.pending_approvals > 0 && me.role === "manager"
              ? "animate-pulse bg-seal text-white"
              : data.pending_approvals > 0
                ? "bg-amber-soft text-amber"
                : "text-ink-faint hover:text-ink"
          }`}
        >
          {data.pending_approvals > 0 ? `待核准 ${data.pending_approvals} 件` : "核准紀錄"} →
        </Link>
      </div>
    </div>
  );
}
