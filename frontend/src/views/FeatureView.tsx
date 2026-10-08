import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAccounts } from "../api/hooks";
import { AccountMenu, Popover, StatusChip, useTokenRenewal, type Menu } from "../components/shell/Chrome";
import { Icon } from "../components/shell/Icons";
import { domainOfPath, MODULE } from "../shell/design";

/** 功能頁的名稱（頁首用） */
const PAGES: [RegExp, string][] = [
  [/^\/(artworks|drawings)\/intake/, "照片建檔"],
  [/^\/artworks\/[^/]+\/chat/, "畫作問答"],
  [/^\/artworks\//, "畫作資料"],
  [/^\/search/, "以文搜畫・以圖搜圖"],
  [/^\/photo-diff/, "比對兩張照片"],
  [/^\/drawings\/search/, "圖紙查找"],
  [/^\/drawings\/[^/]+\/chat/, "圖紙問答"],
  [/\/reconstruct/, "3D 重建"],
  [/^\/drawings\//, "圖紙資料"],
  [/^\/inventory/, "庫存・訂單・工單查詢"],
  [/^\/schedule/, "生產排程"],
  [/^\/approvals/, "主管核准"],
  [/^\/compare-items/, "兩件並排比較"],
  [/^\/batch/, "批次辨識"],
  [/^\/compare/, "策略比較"],
  [/^\/admin/, "系統狀態"],
];

/**
 * 功能頁外框：原本的完整功能頁（/search、/drawings/*、/inventory、/schedule、/approvals、/admin…）網址不變、功能不變，
 * 套上所屬模組的外觀（畫作相關用藝術模組暖色，其他用工廠模組），頁首可以回到上一段對話或入口。
 * 權限一樣由各頁呼叫的 API 檢查（JWT 在 HttpOnly cookie）。
 */
export function FeatureView({ backTo }: { backTo: string | null }) {
  const { pathname, hash } = useLocation();
  const navigate = useNavigate();
  const domain = domainOfPath(pathname);
  const page = PAGES.find(([re]) => re.test(pathname))?.[1] ?? "功能頁";
  const [menu, setMenu] = useState<Menu>(null);
  const { data: accounts } = useAccounts();
  const renewal = useTokenRenewal();
  const scroller = useRef<HTMLElement>(null);
  const close = () => setMenu(null);

  // 進功能頁一律從頂端（或 #錨點）開始；資料還在載入時錨點還不存在，最多等 2 秒
  useLayoutEffect(() => {
    scroller.current?.scrollTo(0, 0);
  }, [pathname]);
  useEffect(() => {
    if (!hash) return;
    let tries = 0;
    const timer = window.setInterval(() => {
      const el = document.getElementById(decodeURIComponent(hash.slice(1)));
      if (el || ++tries > 20) {
        window.clearInterval(timer);
        el?.scrollIntoView({ block: "start" });
      }
    }, 100);
    return () => window.clearInterval(timer);
  }, [pathname, hash]);

  return (
    <div className={`app app--module app--${domain} app--feature`}>
      <header className="mtop">
        <button type="button" className="mtop__back" onClick={() => navigate(backTo ?? "/")} title={backTo ? "回到剛才的對話" : "回到入口"}>
          <Icon name="arrowLeft" strokeWidth={1.6} />
          <span>{backTo ? "回到對話" : "入口"}</span>
        </button>
        <div className="mtop__brand">
          <span className="mtop__name">{MODULE[domain].spaced}</span>
          <span className="mtop__en">{MODULE[domain].en}</span>
        </div>
        <p className="mtop__conv">功能頁・{page}</p>
        <div className="mtop__actions">
          <StatusChip className="mtop__status" />
          <div className="topbar__slot">
            <button type="button" className={`mtop__btn${menu === "account" ? " is-open" : ""}`} onClick={() => setMenu(menu === "account" ? null : "account")} aria-expanded={menu === "account"}>
              <span>{accounts?.current.label ?? "讀取中"}</span>
              {!!accounts?.pending_approvals && <span className="count">{accounts.pending_approvals}</span>}
              <Icon name="chevronDown" strokeWidth={1.8} />
            </button>
            <Popover open={menu === "account"} onClose={close} className="menu--right">
              <AccountMenu onClose={close} />
            </Popover>
          </div>
        </div>
      </header>
      {renewal.renewed && (
        <p className="banner" role="status">
          {renewal.renewed}：已重新取得〈{renewal.label}〉的憑證。
          <button type="button" className="link-btn" onClick={renewal.dismiss}>
            知道了
          </button>
        </p>
      )}
      <main ref={scroller} className="feature">
        <div className="legacy feature__inner">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
