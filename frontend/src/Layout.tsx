import { useEffect, useLayoutEffect, useRef } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import type { MemoryStatus } from "./api/client";
import { useHealth } from "./api/hooks";
import { IdentityBar } from "./components/agent/IdentityBar";
import { AssistantPage } from "./pages/AssistantPage";

/** 功能頁的名稱（只用在「回到智慧助理」那一列；功能頁不放導覽列，只從對話裡的「深入」按鈕進來） */
const PAGES: [RegExp, string][] = [
  [/^\/artworks\/[^/]+\/chat/, "畫作問答"],
  [/^\/artworks\//, "畫作資料"],
  [/^\/search/, "以文搜畫"],
  [/^\/photo-diff/, "比對兩張照片"],
  [/^\/drawings\/search/, "圖紙查找"],
  [/^\/drawings\/[^/]+\/chat/, "圖紙問答"],
  [/\/reconstruct/, "3D 重建"],
  [/^\/drawings\//, "圖紙資料"],
  [/^\/inventory/, "庫存・訂單・工單查詢"],
  [/^\/schedule/, "生產排程"],
  [/^\/approvals/, "主管核准"],
  [/^\/compare/, "策略比較"],
  [/^\/admin/, "系統狀態"],
];

export function Layout() {
  const { data: health } = useHealth();
  const { pathname, hash } = useLocation();
  const home = pathname === "/";
  const degraded = health && health.status !== "ok";
  const page = PAGES.find(([re]) => re.test(pathname))?.[1];

  // 智慧助理一直掛著（hidden 而不卸載）：從「深入」按鈕到功能頁再回來，對話、串流中的回答都還在。
  // 捲動位置自己記：離開對話時記住、回來時還原；進功能頁一律從頂端（或 #錨點）開始。
  const homeRef = useRef(home);
  const homeScroll = useRef(0);
  useEffect(() => {
    const on = () => {
      if (homeRef.current) homeScroll.current = window.scrollY;
    };
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);
  useLayoutEffect(() => {
    homeRef.current = home;
    if (home) {
      window.scrollTo(0, homeScroll.current);
      return;
    }
    window.scrollTo(0, 0);
    if (!hash) return;
    // 功能頁的資料還在載入時錨點還不存在：最多等 2 秒
    let tries = 0;
    const timer = window.setInterval(() => {
      const el = document.getElementById(decodeURIComponent(hash.slice(1)));
      if (el || ++tries > 20) {
        window.clearInterval(timer);
        el?.scrollIntoView({ block: "start" });
      }
    }, 100);
    return () => window.clearInterval(timer);
  }, [home, pathname, hash]);

  return (
    <div className="flex min-h-dvh flex-col">
      {/* global-nav：44px 純黑、12px 白字；下面接毛玻璃的身分列（sub-nav-frosted）。
          統一入口：不放功能切換，畫作、圖紙、3D、庫存、排程都由智慧助理的七段流程分派 */}
      <header className="sticky top-0 z-20">
        <div className="bg-nav text-white">
          <div className="mx-auto flex h-11 max-w-5xl items-center gap-3 px-4">
            <Link to="/" className="flex min-w-0 items-center gap-2 text-white">
              <span className="font-display text-[19px] font-semibold leading-none">畫</span>
              <span className="truncate text-xs text-white/80">地端隱私多模態 RAG 專題</span>
            </Link>
            {degraded && (
              <Link
                to="/admin"
                className="ml-auto shrink-0 rounded-full bg-white/10 px-2.5 py-0.5 text-xs text-warning-on-dark"
              >
                {health.outage_simulated ? "推論伺服器離線" : "部分服務異常"}
              </Link>
            )}
            <MemoryChip memory={health?.memory} pushRight={!degraded} />
          </div>
        </div>
        <IdentityBar />
      </header>

      <main className={`mx-auto w-full max-w-5xl flex-1 px-4 ${home ? "pt-6 sm:pt-8" : "pb-12 pt-4 sm:pt-5"}`}>
        <div hidden={!home}>
          <AssistantPage active={home} />
        </div>
        {!home && (
          <>
            <nav className="mb-4 flex items-center gap-2 text-sm">
              <Link to="/" className="link inline-flex items-center gap-0.5 font-semibold">
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden>
                  <path d="m15 5-7 7 7 7" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                回到智慧助理
              </Link>
              {page && <span className="truncate text-ink-48">· 深入：{page}</span>}
            </nav>
            <Outlet />
          </>
        )}
      </main>
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
      className={`${pushRight ? "ml-auto sm:ml-1" : "ml-1"} hidden shrink-0 rounded-full bg-white/10 px-2.5 py-0.5 text-xs tabular-nums sm:inline-block ${
        recent ? "text-success-on-dark" : high ? "text-warning-on-dark" : "text-white/70"
      }`}
    >
      {recent && last ? `釋放 ${last.released.length} 個模型 · ${last.percent_after}%` : `記憶體 ${Math.round(memory.percent)}%`}
    </Link>
  );
}
