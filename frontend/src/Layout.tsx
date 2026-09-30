import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useHealth } from "./api/hooks";

const NAV = [
  { to: "/", label: "尋畫", icon: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4.3-4.3" },
  { to: "/drawings", label: "工廠圖紙", icon: "M3 21V9l6-4v4l6-4v4l6-4v16zM7 17h2m4 0h2" },
  { to: "/compare", label: "策略比較", icon: "M4 5h7v14H4zM13 5h7v14h-7z" },
  { to: "/admin", label: "系統狀態", icon: "M4 19V9m6 10V5m6 14v-7m4 7H2" },
];

/** 「工廠圖紙」涵蓋 /drawings/* 與 /reconstruct */
const isActive = (to: string, path: string, active: boolean) =>
  to === "/drawings" ? path.startsWith("/drawings") || path.startsWith("/reconstruct") : active;

export function Layout() {
  const { data: health } = useHealth();
  const { pathname } = useLocation();
  const degraded = health && health.status !== "ok";
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-paper/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4">
          <Link to="/" className="flex items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-md bg-seal font-serif text-lg font-black text-white shadow-sm">
              畫
            </span>
            <span className="font-serif text-xl font-black tracking-wide">
              畫語 <span className="text-sm font-bold text-ink-faint">ArtRAG</span>
            </span>
          </Link>
          <nav className="ml-auto hidden gap-1 sm:flex">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                className={({ isActive: a }) =>
                  `rounded-lg px-3 py-1.5 text-sm font-medium transition ${
                    isActive(n.to, pathname, a) ? "bg-ink text-paper" : "text-ink-soft hover:bg-paper-deep"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          {degraded && (
            <Link
              to="/admin"
              className="ml-auto rounded-full bg-amber-soft px-2 py-0.5 text-xs font-bold text-amber sm:ml-2"
            >
              {health.outage_simulated ? "推論伺服器離線" : "部分服務異常"}
            </Link>
          )}
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 pb-24 pt-5 sm:pb-10">
        <Outlet />
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-20 grid grid-cols-4 border-t border-line bg-paper/95 pb-[env(safe-area-inset-bottom)] backdrop-blur sm:hidden">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            className={({ isActive: a }) =>
              `flex flex-col items-center gap-0.5 py-2 text-[11px] font-medium ${
                isActive(n.to, pathname, a) ? "text-seal" : "text-ink-faint"
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
