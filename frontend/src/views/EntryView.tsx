import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAccounts, useSwitchAccount } from "../api/hooks";
import { AccountMenu, Popover, SecurityPanel, StatusChip, useTokenRenewal, type Menu } from "../components/shell/Chrome";
import { Composer } from "../components/shell/Composer";
import { Home, type GUARD_EXAMPLES, type SCRIPT } from "../components/shell/Home";
import { Icon } from "../components/shell/Icons";
import { JumpCard } from "../components/shell/Jump";
import { Sidebar } from "../components/shell/Sidebar";
import { Thread } from "../components/shell/Thread";
import { COPY, type Domain } from "../shell/design";
import { useEscape, useMedia, useStickyScroll } from "../shell/hooks";
import { ctaOf, deriveOutputs } from "../shell/outputs";
import { titleOf } from "../shell/persist";
import { useShell } from "../shell/store";
import type { Conv } from "../shell/types";
import { forgeToken, useThreadActions } from "./shared";

/**
 * 來源：ArtRAG-前端demo/source/src/views/EntryView.tsx。
 * 入口（01 Quiet 風格）：左側對話紀錄（可收合；手機是抽屜）、問候與輸入框、建議問句。
 * 問了之後只回簡介；七段流程分派到工廠或藝術，就在簡介下方放那個模組的按鈕（沒分派到模組就不放）。
 */
export function EntryView({ convId, onJump }: { convId: string | null; onJump: (convId: string, domain: Domain, key?: string) => void }) {
  const shell = useShell();
  const navigate = useNavigate();
  const conv = shell.convs.find((c) => c.id === convId) ?? null;
  const [menu, setMenu] = useState<Menu>(null);
  const [drawer, setDrawer] = useState(false);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  useEscape(drawer, closeDrawer);
  const wide = useMedia("(min-width: 768px)");
  const desktop = useMedia("(min-width: 1024px)");
  const empty = !conv || conv.turns.length === 0;
  const heroComposer = empty && wide;
  const sticky = useStickyScroll(conv?.id ?? null, !empty);
  const busy = !!conv?.turns.some((t) => t.phase === "routing" || t.phase === "running");
  const { data: accounts } = useAccounts();
  const switchAccount = useSwitchAccount();
  const renewal = useTokenRenewal();
  const close = () => setMenu(null);

  const ask = (text: string, imageId: string | null = null, forced: string | null = null, token?: string) => {
    const id = shell.ask(conv?.id ?? null, "entry", { text, imageId, forced, token });
    if (!conv) navigate(`/c/${id}`);
    sticky.follow();
  };
  const actions = useThreadActions(conv, "brief", (q, forced, imageId) => ask(q, imageId ?? null, forced ?? null));
  const outputs = deriveOutputs(conv);

  const onGuard = (x: (typeof GUARD_EXAMPLES)[number]) => ask(x.q, null, null, x.tamper && accounts ? forgeToken(accounts.token.unsigned) : undefined);
  const onScript = async (s: (typeof SCRIPT)[number]) => {
    if (accounts?.current.id !== s.account) await switchAccount(s.account);
    if (s.to) navigate(s.to);
    else ask(s.q);
  };
  const remove = (c: Conv) => {
    if (!window.confirm(`刪除這段對話紀錄？\n「${titleOf(c)}」\n\n只會刪掉這台電腦上的紀錄。`)) return;
    shell.remove(c.id);
    if (c.id === conv?.id) navigate("/");
  };

  const composer = <Composer busy={busy} copy={COPY.entry} onSend={(t, img) => ask(t, img)} onStop={() => conv && shell.stop(conv.id)} autoFocus key={heroComposer ? "hero" : "dock"} />;
  const sidebar = (variant: "rail" | "drawer") => (
    <Sidebar
      convs={shell.convs}
      activeId={conv?.id ?? null}
      collapsed={shell.collapsed}
      variant={variant}
      onToggle={() => shell.setCollapsed(!shell.collapsed)}
      onNew={() => {
        setDrawer(false);
        navigate("/");
      }}
      onOpen={(c) => {
        setDrawer(false);
        navigate(c.module ? `/${c.module}/${c.id}` : `/c/${c.id}`);
      }}
      onDelete={remove}
      onClose={() => setDrawer(false)}
    />
  );

  return (
    <div className="app app--entry" data-state={empty ? "home" : "chat"} data-collapsed={shell.collapsed || undefined}>
      {desktop && sidebar("rail")}
      {!desktop && drawer && (
        <div className="drawer" role="dialog" aria-modal="true" aria-label="對話紀錄">
          <div className="drawer__scrim" onClick={() => setDrawer(false)} />
          {sidebar("drawer")}
        </div>
      )}
      <div className="main">
        <header className="topbar">
          {!desktop && (
            <button type="button" className="topbar__btn topbar__icon" onClick={() => setDrawer(true)} title="對話紀錄" aria-label="打開對話紀錄">
              <Icon name="menu" />
            </button>
          )}
          <span className="topbar__crumb topbar__crumb--title">{conv ? titleOf(conv) : ""}</span>
          <div className="topbar__actions">
            <StatusChip className="topbar__status" />
            <div className="topbar__slot">
              <button
                type="button"
                className={`topbar__btn topbar__security${menu === "security" ? " is-open" : ""}`}
                onClick={() => setMenu(menu === "security" ? null : "security")}
                aria-expanded={menu === "security"}
              >
                <Icon name="shield" />
                <span className="topbar__btn-label">七段防護</span>
              </button>
              <Popover open={menu === "security"} onClose={close} className="menu--wide menu--security">
                <SecurityPanel />
              </Popover>
            </div>
            <div className="topbar__slot">
              <button
                type="button"
                className={`topbar__btn topbar__account${menu === "account" ? " is-open" : ""}`}
                onClick={() => setMenu(menu === "account" ? null : "account")}
                aria-expanded={menu === "account"}
              >
                <span className="avatar">{(accounts?.current.label ?? "訪").slice(0, 1)}</span>
                <span className="topbar__btn-label">{accounts?.current.label ?? "讀取中"}</span>
                {!!accounts?.pending_approvals && <span className="count">{accounts.pending_approvals}</span>}
                <Icon name="chevronDown" strokeWidth={2} />
              </button>
              <Popover open={menu === "account"} onClose={close} className="menu--right">
                <AccountMenu onClose={close} />
              </Popover>
            </div>
            <button
              type="button"
              className="topbar__btn topbar__icon"
              onClick={() => shell.setTheme(shell.theme === "dark" ? "light" : "dark")}
              title={shell.theme === "dark" ? "淺色" : "深色"}
              aria-label={shell.theme === "dark" ? "切換成淺色" : "切換成深色"}
            >
              <Icon name={shell.theme === "dark" ? "sun" : "moon"} />
            </button>
            {!desktop && (
              <button type="button" className="topbar__btn topbar__icon" onClick={() => navigate("/")} disabled={empty} title="新對話" aria-label="新對話">
                <Icon name="newChat" />
              </button>
            )}
          </div>
        </header>
        {renewal.renewed && (
          <p className="banner" role="status">
            {renewal.renewed}：閘道不再接受舊憑證，已重新取得〈{renewal.label}〉的憑證（要用其他身分請重新切換）。
            <button type="button" className="link-btn" onClick={renewal.dismiss}>
              知道了
            </button>
          </p>
        )}
        <div className="body">
          <main ref={sticky.scroller} onScroll={sticky.onScroll} className="scroll">
            {empty ? (
              <Home composer={heroComposer ? composer : null} onPick={(p) => ask(p)} onGuard={onGuard} onScript={(s) => void onScript(s)} />
            ) : (
              <div ref={sticky.content} className="thread">
                <Thread
                  conv={conv}
                  actions={actions}
                  afterOf={(t) => {
                    const cta = ctaOf(t);
                    if (!cta) return null;
                    const keys = (outputs.byTurn[t.id] ?? []).filter((k) => outputs.list.find((o) => o.key === k)?.domain === cta.domain);
                    const first = keys.find((k) => /^(drawing|artwork):/.test(k)) ?? keys.at(-1);
                    return <JumpCard domain={cta.domain} line={cta.line} onGo={() => onJump(conv.id, cta.domain, first)} />;
                  }}
                />
              </div>
            )}
          </main>
          {!heroComposer && (
            <div className="dock">
              <div className="dock__inner">
                {!empty && !sticky.atBottom && (
                  <button type="button" className="dock__down fade-in" onClick={sticky.toBottom} title="捲到最新" aria-label="捲到最新">
                    <Icon name="arrowDown" strokeWidth={2} />
                  </button>
                )}
                {composer}
                <p className="dock__note">回答可能有誤，數字請以系統為準。七段流程會判斷問題屬於工廠或藝術，再給你進入模組的按鈕。</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
