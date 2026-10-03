import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { type Account, type TokenInfo } from "../../api/client";
import { useAccounts, useSwitchAccount } from "../../api/hooks";

/** 這個身分可以改什麼、範圍到哪 */
export function scopeText(a: Account) {
  if (a.role === "guest") return "只能查詢，不能修改";
  if (a.role === "warehouse") return `盤點、調撥、報廢、庫存狀態 · ${a.warehouses.join("、")}`;
  if (a.role === "sales") return `訂單交期、數量 · ${a.customers.join("、")}`;
  if (a.role === "planner") return "開立工單、改交期、取消自己開的工單、執行排程 · 全廠";
  if (a.role === "manager") return "核准超額申請（不能核准自己的）";
  return a.note;
}

const hhmm = (iso: string) => iso.slice(11, 16);

/**
 * 展示版身分列：頁首下方切換預設帳號（不用密碼）。切換時後端簽發 JWT（HS256，放在 HttpOnly cookie），
 * 七段權限控管的第 1 段由 API 閘道驗它的簽章與效期（docs/adr/015）。
 * 同一句話換個身分就會被拒絕——權限由憑證決定，模型決定不了。
 */
export function IdentityBar() {
  const switchAccount = useSwitchAccount();
  const qc = useQueryClient();
  const { data } = useAccounts();
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [renewed, setRenewed] = useState<string | null>(null);

  // 憑證過期或後端重啟（簽章金鑰換了）：client.ts 已重新取得訪客憑證，這裡提示並重抓所有資料
  useEffect(() => {
    const on = (e: Event) => {
      const code = (e as CustomEvent<string>).detail;
      setRenewed(code === "TOKEN_EXPIRED" ? "憑證過期" : code === "TOKEN_STALE" ? "後端重啟過，舊憑證失效" : "沒有憑證");
      void qc.invalidateQueries();
    };
    window.addEventListener("artrag:token-renewed", on);
    return () => window.removeEventListener("artrag:token-renewed", on);
  }, [qc]);

  // 頁首每 10 秒重抓身分：舊憑證失效時 /auth/accounts 會直接改發訪客憑證（不回 401），這裡也要提示
  const issued = data?.token.checks.find((c) => c.key === "issued");
  const issuedDetail = issued && !issued.detail.startsWith("還沒有憑證") ? issued.detail : null;
  useEffect(() => {
    if (!issuedDetail) return;
    setRenewed(issuedDetail.split("，")[0]);
    void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] !== "accounts" });
  }, [issuedDetail, qc]);

  if (!data) return null;
  const me = data.current;
  const token = data.token;

  const change = async (id: string) => {
    setBusy(true);
    setRenewed(null);
    try {
      await switchAccount(id);
    } finally {
      setBusy(false);
    }
  };

  // sub-nav-frosted：Parchment 毛玻璃；右側固定一個動作（主管有待核准時是藍色藥丸）
  return (
    <div className="frosted border-b border-hairline">
      <div className="mx-auto flex min-h-11 max-w-5xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5 text-[13px]">
        <span className="text-ink-48">目前身分</span>
        {data.demo_controls ? (
          <select
            value={me.id}
            disabled={busy}
            onChange={(e) => void change(e.target.value)}
            aria-label="切換展示身分"
            className="rounded-full border border-hairline bg-canvas py-1 pl-3 pr-7 text-[13px] font-semibold text-ink outline-none focus:border-accent-focus"
          >
            {data.accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
        ) : (
          <span className="rounded-full border border-hairline bg-canvas px-3 py-1 font-semibold">{me.label}</span>
        )}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          title="第 1 段：API 閘道驗這張 JWT 的簽章與效期"
          className="chip gap-1.5 py-0.5 font-mono text-[12px] text-ink-80 hover:border-accent hover:text-accent"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-success" />
          JWT · clearance {me.clearance} · {me.dept} · 到 {hhmm(token.expires_at)}
        </button>
        <span className="min-w-0 truncate text-ink-80">{scopeText(me)}</span>
        <Link
          to="/approvals"
          className={`ml-auto shrink-0 ${
            data.pending_approvals > 0 && me.role === "manager"
              ? "btn-primary px-3.5 py-1 text-[13px]"
              : data.pending_approvals > 0
                ? "font-semibold text-warning"
                : "link"
          }`}
        >
          {data.pending_approvals > 0 ? `待核准 ${data.pending_approvals} 件` : "核准紀錄"} ›
        </Link>
        {renewed && (
          <p className="w-full text-[12px] text-warning">
            {renewed}：閘道不再接受舊憑證，已重新取得〈{me.label}〉的憑證（要用其他身分請重新切換）。
          </p>
        )}
        {open && <TokenPanel token={token} />}
      </div>
    </div>
  );
}

/** 憑證內容：閘道驗了什麼、payload（簽章不回傳到畫面） */
function TokenPanel({ token }: { token: TokenInfo }) {
  return (
    <div className="mb-1 grid w-full gap-3 rounded-lg border border-hairline bg-canvas p-3 text-[12px] sm:grid-cols-2">
      <div>
        <p className="font-semibold text-ink">第 1 段・API 閘道驗證</p>
        <ul className="mt-1 flex flex-col gap-0.5 text-ink-80">
          {token.checks.map((c) => (
            <li key={c.key} className="flex gap-2">
              <span className={c.ok ? "text-success" : "text-danger"}>{c.ok ? "✓" : "✗"}</span>
              <span className="w-8 shrink-0 text-ink-48">{c.label}</span>
              <span>{c.detail}</span>
            </li>
          ))}
          {token.checks.length === 0 && <li className="text-ink-48">剛簽發的憑證，下一個請求起由閘道驗證</li>}
        </ul>
        <p className="mt-2 text-ink-48">
          {token.alg}・存在 HttpOnly cookie（JavaScript 讀不到）・沒帶、簽章不符或過期一律 401，請求碰不到任何模型與資料。
          第 3 段的 Metadata Filter 只照這張憑證的 clearance 與 depts 產生。
        </p>
      </div>
      <pre className="max-h-48 overflow-auto rounded-lg bg-tile p-2.5 font-mono text-[11px] leading-relaxed text-[#e0e0e0]">
        {`// JWT payload（檢索權限的唯一來源）\n${JSON.stringify(token.claims, null, 2)}`}
      </pre>
    </div>
  );
}
