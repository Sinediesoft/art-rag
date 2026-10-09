import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Account, AccountsResponse, MemoryStatus } from "../../api/client";
import { useAccounts, useCanView, useSecurityLogs, useStatus, useSwitchAccount } from "../../api/hooks";
import { acknowledgeWrite, dismissOrphans, useWrites } from "../../api/writes";
import { formatTaipei } from "../../lib/format";
import { Icon } from "./Icons";

export type Menu = "security" | "account" | "tools" | null;

/** 來源：ArtRAG-前端demo/source/src/components/Chrome.tsx。從觸發按鈕垂下的選單；點外面或 Esc 關閉 */
export function Popover({ open, onClose, children, className = "" }: { open: boolean; onClose: () => void; children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => !ref.current?.parentElement?.contains(e.target as Node) && onClose();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", h);
    document.addEventListener("keydown", k);
    return () => {
      document.removeEventListener("mousedown", h);
      document.removeEventListener("keydown", k);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div ref={ref} className={`menu menu--down ${className}`} role="menu">
      {children}
    </div>
  );
}

/** 這個身分可以改什麼、範圍到哪 */
export function scopeText(a: Account) {
  if (a.role === "guest") return "只能查詢，不能修改";
  if (a.role === "warehouse") return `盤點、調撥、報廢、庫存狀態・${a.warehouses.join("、")}`;
  if (a.role === "sales") return `訂單交期、數量・${a.customers.join("、")}`;
  if (a.role === "planner") return "開立工單、改交期、取消自己開的工單、執行排程・全廠";
  if (a.role === "manager") return "核准超額申請（不能核准自己的）、收錄照片建檔的圖紙與藝術家的區域解說";
  if (a.role === "artist") return `在畫上圈區域、寫解說（送主管收錄）・${a.artworks.join("、")}`;
  return a.note;
}

export const pendingCount = (data?: AccountsResponse) =>
  (data?.pending_approvals ?? 0) + (data?.pending_region_drafts ?? 0);

const hhmm = (iso: string) => iso.slice(11, 16);

/**
 * 身分：JWT 放在 HttpOnly cookie（JavaScript 讀不到），第 1 段由 API 閘道驗簽章與效期（docs/adr/015）。
 * 只有後端開了展示模式（DEMO_CONTROLS，docs/adr/030）才能不用密碼切換身分；沒開時只顯示目前身分。
 */
export function AccountMenu({ onClose }: { onClose: () => void }) {
  const { data } = useAccounts();
  const switchAccount = useSwitchAccount();
  const writing = useWrites().some((w) => w.status === "pending");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!data) return <p className="menu__label">讀取身分中…</p>;
  const me = data.current;
  const change = async (id: string) => {
    if (id === me.id) return onClose();
    setBusy(true);
    setErr(null);
    try {
      await switchAccount(id);
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="acct">
        <p className="acct__name">{me.label}</p>
        <p className="acct__scope">{scopeText(me)}</p>
        <p className="acct__jwt num">
          JWT {data.token.alg}・clearance {me.clearance}・{me.dept}・到 {hhmm(data.token.expires_at)}
        </p>
        {data.demo_controls && <p className="acct__warn">展示模式・不可用於正式環境</p>}
      </div>
      {data.demo_controls ? (
        <>
          <p className="menu__label">切換展示身分・權限由伺服器依 JWT 判定</p>
          {writing && <p className="menu__label is-error">有寫入還沒收到結果，等結果回來再切換身分</p>}
          {data.accounts.map((a) => (
            <button
              key={a.id}
              type="button"
              disabled={busy || (writing && me.id !== a.id)}
              className={`menu__item${me.id === a.id ? " is-selected" : ""}`}
              onClick={() => void change(a.id)}
              role="menuitemradio"
              aria-checked={me.id === a.id}
            >
              <span className="menu__text">
                {a.label}
                <small>{a.role_label}・clearance {a.clearance}</small>
              </span>
              <Icon name="check" className="menu__check" strokeWidth={2.2} />
            </button>
          ))}
        </>
      ) : (
        <p className="menu__label">正式模式：身分由登入決定，畫面上不能切換</p>
      )}
      {err && <p className="menu__label is-error">{err}</p>}
      <Link to="/approvals" className="menu__item" onClick={onClose}>
        <span className="menu__text">
          {pendingCount(data) > 0 ? `待核准 ${pendingCount(data)} 件` : "核准紀錄"}
          <small>異動與主管核准</small>
        </span>
        <Icon name="chevronRight" strokeWidth={2} />
      </Link>
    </>
  );
}

const LAYERS = [
  { title: "認證與授權", tech: "JWT＋角色", desc: "API 閘道驗 JWT 的簽章與效期，沒帶、竄改、過期一律 401；再用憑證裡的角色檢查要做的事。" },
  { title: "Jev Choice", tech: "意圖路由／防護欄", desc: "判斷正常查詢、Prompt 注入或閒聊：注入攔截並記錄、閒聊快速短路。叫不到 Jev 時只靠地端規則。" },
  { title: "Metadata Filter", tech: "權限感知檢索", desc: "只照 JWT 的 clearance 與部門產生條件，看不到的文件塊在資料庫查詢時就被濾掉。" },
  { title: "Jev Noul", tech: "雙重驗證", desc: "每段做 is_relevant 與 security_leak_check，任一不通過就剔除；內部與機密段落改在地端判斷。" },
  { title: "Jev Score", tech: "評分重排", desc: "通過的段落依幫助程度打分、取前 3 段。" },
  { title: "生成閘門", tech: "Generation Gate", desc: "權限內的資料能回答且合規才放行；否則降級回「查無資料」，不透露有文件但你沒有權限。" },
  { title: "本地 LLM", tech: "System 2", desc: "只依留下的段落在本機生成，回覆送出前再做輸出檢查。" },
];

/** 安全防護：七段說明、Jev 狀態（伺服器決定，畫面上不能切換）、今天的拒絕並記錄 */
export function SecurityPanel() {
  const { data: status } = useStatus();
  const canLogs = useCanView("security_logs");
  const { data: logs } = useSecurityLogs(8);
  const s1 = status?.system1;
  return (
    <div className="security">
      <div className="security__head">
        <span className={`status-dot ${status?.status === "ok" ? "is-ok" : "is-warn"}`} />
        <span className="security__title">七段權限控管</span>
        <span className="security__state">{status ? (status.status === "ok" ? "服務正常" : status.outage_simulated ? "推論伺服器離線（模擬）" : "部分服務異常") : "讀取中"}</span>
      </div>
      <p className="security__lede">每句話依序經過七段；機密資料只在本機處理。第 2、4～6 段由誰判斷由伺服器決定。</p>
      <ol className="layers">
        {LAYERS.map((l, i) => (
          <li key={l.title} className="layers__item">
            <span className="layers__n">{i + 1}</span>
            <span className="layers__body">
              <span className="layers__title">
                {l.title}
                <small>{l.tech}</small>
              </span>
              <span className="layers__desc">{l.desc}</span>
            </span>
          </li>
        ))}
      </ol>
      {s1 && (
        <p className="security__note">
          {s1.jev_configured ? `雲端 Jev ${s1.model}：只收代號化文字，內部與機密段落一律不出廠；叫不到時只靠地端規則。` : `Jev 未啟用（${s1.detail}）：第 2、4～6 段由地端規則判斷。`}
        </p>
      )}
      <div className="security__log">
        {canLogs && logs ? (
          <>
            <p className="security__label">
              <span>今天擋下／剔除</span>
              <span className="num">{(logs.today.rbac ?? 0) + (logs.today.guard ?? 0) + (logs.today.post ?? 0)} 筆</span>
            </p>
            {logs.items.length ? (
              <ul className="log">
                {logs.items.slice(0, 8).map((l) => (
                  <li key={l.no} className="log__item">
                    <span className="log__no">{l.no}</span>
                    <span className="log__rule">
                      第 {l.stage} 段・{l.rule}
                    </span>
                    <span className="log__at">{formatTaipei(l.created_at).slice(-5)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="empty">今天還沒有擋下或剔除的紀錄。</p>
            )}
          </>
        ) : (
          <p className="empty">拒絕並記錄的明細只有能看安全紀錄的身分看得到（access.yaml 的 views）。</p>
        )}
      </div>
    </div>
  );
}

/**
 * 憑證自動更新的提示：client.ts 在 401（沒有、過期、後端重啟）時重新取得訪客憑證並重送，這裡提示並重抓所有查詢。
 * 簽章不符（被竄改）不重送，由那一輪顯示閘道拒絕。
 */
export function useTokenRenewal() {
  const qc = useQueryClient();
  const [renewed, setRenewed] = useState<string | null>(null);
  const { data } = useAccounts();
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
  return { renewed, label: data?.current.label, dismiss: () => setRenewed(null) };
}

const WRITE_STATUS = {
  pending: "還在等伺服器回覆",
  done: "伺服器回覆已完成",
  failed: "伺服器回覆沒有完成",
  unknown: "連線中斷，結果未確認",
} as const;

/**
 * 身分改變時還沒收到結果的寫入（api/writes.ts）：伺服器可能已經寫好，結果不接回原畫面，
 * 這裡只說「哪一種寫入、伺服器回覆了沒有」，不顯示內容；提醒到紀錄頁以目前身分核對、不要直接重送
 */
export function WriteNotice() {
  const writes = useWrites();
  // 身分改變時送出、已經有確定結果（或還在等）的：說明伺服器回覆了沒有
  const orphans = writes.filter((w) => w.orphaned && w.status !== "unknown");
  // 結果未確認的（這個頁面、其他分頁、重新載入前送出的都算）：可能已經完成，核對後才能再送同一件事
  const unknown = writes.filter((w) => w.status === "unknown");
  if (!orphans.length && !unknown.length) return null;
  const waiting = orphans.some((w) => w.status === "pending");
  return (
    <>
      {orphans.length > 0 && (
        <div className="banner" role="alert">
          <span>
            身分改變時有 {orphans.length} 筆寫入已經送出：
            {orphans.map((w) => `〈${w.label}〉${WRITE_STATUS[w.status]}`).join("；")}。結果不會接回原畫面，請以目前身分到
            <Link to="/approvals">核准紀錄</Link>或<Link to="/inventory">庫存・工單</Link>核對，不要直接重送。
          </span>
          {!waiting && (
            <button type="button" className="link-btn" onClick={dismissOrphans}>
              知道了
            </button>
          )}
        </div>
      )}
      {unknown.length > 0 && (
        <div className="banner banner--unconfirmed" role="alert">
          <span>
            有 {unknown.length} 筆寫入送出後結果未確認（連線中斷、伺服器錯誤，或送出中頁面就關掉了），可能已經完成：
            請以目前身分到<Link to="/approvals">核准紀錄</Link>或<Link to="/schedule">生產排程</Link>、
            <Link to="/inventory">庫存・工單</Link>核對。核對後按「已核對」，同一件事才能再送。
          </span>
          {unknown.map((w) => (
            <button key={w.id} type="button" className="link-btn" onClick={() => acknowledgeWrite(w.id)} aria-label={`已核對〈${w.label}〉`}>
              〈{w.label}〉已核對
            </button>
          ))}
        </div>
      )}
    </>
  );
}

/** 服務狀態與記憶體（GET /status，要 JWT）；剛釋放過模型就顯示釋放了幾個 */
export function StatusChip({ className = "" }: { className?: string }) {
  const { data } = useStatus();
  if (!data) return null;
  const memory: MemoryStatus | null | undefined = data.memory;
  const degraded = data.status !== "ok";
  const last = memory?.events.find((e) => e.released.length > 0);
  const recent = last && Date.now() - new Date(last.at).getTime() < 60_000;
  const high = memory && (memory.percent >= memory.threshold || (!!memory.gpu && memory.gpu.percent >= memory.gpu.threshold));
  const text = degraded
    ? data.outage_simulated
      ? "推論伺服器離線"
      : "部分服務異常"
    : recent && last
      ? `釋放 ${last.released.length} 個模型・${last.percent_after}%`
      : memory
        ? `記憶體 ${Math.round(memory.percent)}%${memory.gpu ? `・VRAM ${Math.round(memory.gpu.percent)}%` : ""}`
        : "服務正常";
  return (
    <Link
      to="/admin#memory"
      className={`statuschip ${degraded || high ? "is-warn" : recent ? "is-ok" : ""} ${className}`}
      title={memory ? `系統記憶體 ${memory.percent}%（超過 ${memory.threshold}% 會釋放目前流程用不到的模型）` : "系統狀態"}
    >
      <span className="statuschip__dot" />
      <span className="num">{text}</span>
    </Link>
  );
}
