import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import type { MemoryStatus } from "./api/client";
import { useAccounts, useHealth } from "./api/hooks";
import { IdentityBar } from "./components/agent/IdentityBar";

/** domain：這一頁要讀的資料領域（docs/adr/012 資料範圍）；目前身分不能讀就在導覽列標鎖頭 */
const NAV: { to: string; label: string; icon: string; domain?: string }[] = [
  { to: "/assistant", label: "智慧助理", icon: "M4 5h16v11H8l-4 4zM8 10h.01M12 10h.01M16 10h.01" },
  { to: "/", label: "尋畫", icon: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4.3-4.3" },
  { to: "/drawings", label: "工廠圖紙", icon: "M3 21V9l6-4v4l6-4v4l6-4v16zM7 17h2m4 0h2", domain: "mfg" },
  {
    to: "/inventory",
    label: "庫存查詢",
    icon: "M3 7.5 12 3l9 4.5v9L12 21l-9-4.5zM3 7.5l9 4.5 9-4.5M12 12v9",
    domain: "factory",
  },
  {
    to: "/schedule",
    label: "生產排程",
    icon: "M3 5h18M3 19h18M5 9h7v3H5zM10 14h9v3h-9zM14 9h5v3h-5",
    domain: "factory",
  },
  { to: "/compare", label: "策略比較", icon: "M4 5h7v14H4zM13 5h7v14h-7z" },
  { to: "/admin", label: "系統狀態", icon: "M4 19V9m6 10V5m6 14v-7m4 7H2" },
];

/** 「工廠圖紙」涵蓋 /drawings/* 與 /reconstruct */
const isActive = (to: string, path: string, active: boolean) =>
  to === "/drawings" ? path.startsWith("/drawings") || path.startsWith("/reconstruct") : active;

export function Layout() {
  const { data: health } = useHealth();
  const { data: accounts } = useAccounts();
  const { pathname } = useLocation();
  const locked = (domain?: string) => !!domain && !!accounts && !accounts.current.domains.includes(domain);
  const lockTitle = (domain?: string) =>
    locked(domain) ? `目前身分「${accounts?.current.label}」的資料範圍不含這一頁，請在下方切換身分` : undefined;
  const degraded = health && health.status !== "ok";
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-paper/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4">
          <Link to="/" className="flex min-w-0 items-center gap-2">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-seal font-serif text-lg font-black text-white shadow-sm">
              畫
            </span>
            <span className="truncate font-serif text-base font-black sm:text-lg sm:tracking-wide md:text-xl">
              地端隱私多模態 RAG 專題
            </span>
          </Link>
          <nav className="ml-auto hidden shrink-0 gap-0.5 sm:flex md:gap-1">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                title={lockTitle(n.domain)}
                className={({ isActive: a }) =>
                  `whitespace-nowrap rounded-lg px-2 py-1.5 text-sm font-medium transition md:px-3 ${
                    isActive(n.to, pathname, a)
                      ? "bg-ink text-paper"
                      : locked(n.domain)
                        ? "text-ink-faint/70 hover:bg-paper-deep"
                        : "text-ink-soft hover:bg-paper-deep"
                  }`
                }
              >
                {locked(n.domain) && <span aria-label="資料範圍外">🔒 </span>}
                {n.label}
              </NavLink>
            ))}
          </nav>
          {degraded && (
            <Link
              to="/admin"
              className="ml-auto shrink-0 rounded-full bg-amber-soft px-2 py-0.5 text-xs font-bold text-amber sm:ml-2"
            >
              {health.outage_simulated ? "推論伺服器離線" : "部分服務異常"}
            </Link>
          )}
          <MemoryChip memory={health?.memory} pushRight={!degraded} />
        </div>
        <IdentityBar />
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 pb-24 pt-5 sm:pb-10">
        <Outlet />
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-20 grid grid-cols-7 border-t border-line bg-paper/95 pb-[env(safe-area-inset-bottom)] backdrop-blur sm:hidden">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            title={lockTitle(n.domain)}
            className={({ isActive: a }) =>
              `flex flex-col items-center gap-0.5 py-2 text-[11px] font-medium ${
                isActive(n.to, pathname, a) ? "text-seal" : locked(n.domain) ? "text-ink-faint/40" : "text-ink-faint"
              }`
            }
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
              <path d={n.icon} strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {n.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

/** 記憶體使用率；剛釋放過模型（1 分鐘內）就顯示釋放了幾個，點進系統狀態看明細 */
function MemoryChip({ memory, pushRight }: { memory: MemoryStatus | null | undefined; pushRight: boolean }) {
  if (!memory) return null;
  const last = memory.events.find((e) => e.released.length > 0);
  const recent = last && Date.now() - new Date(last.at).getTime() < 60_000;
  const high = memory.percent >= memory.threshold;
  return (
    <Link
      to="/admin#memory"
      title={
        recent && last
          ? `${last.trigger}：已釋放 ${last.released.map((r) => r.label).join("、")}（${last.percent_before}% → ${last.percent_after}%）`
          : `系統記憶體 ${memory.percent}%（超過 ${memory.threshold}% 會釋放目前流程用不到的模型）`
      }
      className={`${pushRight ? "ml-auto sm:ml-2" : "ml-1"} hidden shrink-0 rounded-full px-2 py-0.5 font-mono text-xs font-bold sm:inline-block ${
        recent ? "bg-jade-soft text-jade" : high ? "bg-amber-soft text-amber" : "bg-paper-deep text-ink-faint"
      }`}
    >
      {recent && last ? `釋放 ${last.released.length} 個模型 · ${last.percent_after}%` : `記憶體 ${Math.round(memory.percent)}%`}
    </Link>
  );
}
