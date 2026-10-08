import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { useAccounts } from "../api/hooks";
import { AccountMenu, Popover, StatusChip, useTokenRenewal, type Menu } from "../components/shell/Chrome";
import { Composer } from "../components/shell/Composer";
import { Icon } from "../components/shell/Icons";
import { JumpCard } from "../components/shell/Jump";
import { Showcase } from "../components/shell/Showcase";
import { Sidebar } from "../components/shell/Sidebar";
import { Thread } from "../components/shell/Thread";
import { COPY, MODULE, type Domain } from "../shell/design";
import { useEscape, useMedia, useStickyScroll } from "../shell/hooks";
import { ctaOf, deriveOutputs, domainOf, subjectFor, withSubject } from "../shell/outputs";
import { titleOf } from "../shell/persist";
import { egressOf, progressOf } from "../shell/stages";
import { useShell } from "../shell/store";
import type { Conv } from "../shell/types";
import { useThreadActions } from "./shared";

/** 模組裡的功能頁捷徑：不是功能選單，是原本「深入」能到的完整頁面（照片建檔、批次辨識、核准…都還在） */
const TOOLS: Record<Domain, { label: string; to: string }[]> = {
  factory: [
    { label: "圖紙查找", to: "/drawings/search" },
    { label: "3D 重建", to: "/reconstruct" },
    { label: "庫存・訂單・工單查詢", to: "/inventory" },
    { label: "生產排程", to: "/schedule" },
    { label: "主管核准", to: "/approvals" },
    { label: "照片建檔（圖紙）", to: "/drawings/intake" },
    { label: "批次辨識", to: "/batch" },
    { label: "兩件並排比較", to: "/compare-items" },
    { label: "系統狀態", to: "/admin" },
  ],
  art: [
    { label: "以文搜畫・以圖搜圖", to: "/search" },
    { label: "比對兩張照片", to: "/photo-diff" },
    { label: "照片建檔（畫作）", to: "/artworks/intake" },
    { label: "批次辨識", to: "/batch" },
    { label: "兩件並排比較", to: "/compare-items" },
    { label: "策略比較", to: "/compare" },
    { label: "系統狀態", to: "/admin" },
  ],
};

/**
 * 來源：ArtRAG-前端demo/source/src/views/ModuleView.tsx。
 * 模組（10 Night 的版面）：左半邊是延續入口的對話，右半邊是對話產生的成果。
 * 左右預設各一半，中間的分隔線可以拖曳（30%–70%）、方向鍵微調，雙擊回到一半；窄螢幕改成「對話／展示區」兩個分頁。
 */
export function ModuleView({ conv, domain, onJump }: { conv: Conv; domain: Domain; onJump: (convId: string, domain: Domain, key?: string) => void }) {
  const shell = useShell();
  const navigate = useNavigate();
  const m = MODULE[domain];
  const outputs = deriveOutputs(conv);
  const outs = outputs.list.filter((o) => o.domain === domain);
  const activeOut = outs.find((o) => o.key === conv.active[domain]) ?? outs[outs.length - 1];
  const subject = subjectFor(domain, activeOut, conv);
  const narrow = !useMedia("(min-width: 900px)");
  const [pane, setPane] = useState<"chat" | "show">("chat");
  const [seen, setSeen] = useState(activeOut?.key);
  const [drawer, setDrawer] = useState(false);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  useEscape(drawer, closeDrawer);
  const [menu, setMenu] = useState<Menu>(null);
  const sticky = useStickyScroll(conv.id, true);
  const body = useRef<HTMLDivElement>(null);
  const { data: accounts } = useAccounts();
  const renewal = useTokenRenewal();
  const busy = conv.turns.some((t) => t.phase === "routing" || t.phase === "running");
  const close = () => setMenu(null);

  useEffect(() => {
    if (pane === "show") setSeen(activeOut?.key);
  }, [pane, activeOut?.key]);

  /** 說「這張圖／這幅畫」時帶入正在看的對象（後端一句一句判斷，沒有上下文） */
  const ask = (text: string, imageId: string | null = null, forced: string | null = null) => {
    shell.ask(conv.id, domain, { text: withSubject(text, subject), imageId, forced });
    sticky.follow();
  };
  const actions = useThreadActions(conv, "full", (q, forced, imageId) => ask(q, imageId ?? null, forced ?? null), domain);
  const select = (key: string) => shell.setActive(conv.id, domain, key);
  const last = conv.turns.at(-1);
  const egress = last?.route ? egressOf(last.route, progressOf(last.part)).bytes : 0;

  const drag = (e: React.PointerEvent) => {
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture?.(e.pointerId);
    const move = (ev: PointerEvent) => {
      const r = body.current!.getBoundingClientRect();
      if (r.width > 0) shell.setSplit(((ev.clientX - r.left) / r.width) * 100);
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      document.body.classList.remove("is-dragging");
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    document.body.classList.add("is-dragging");
  };

  return (
    <div className={`app app--module app--${domain}`}>
      <header className="mtop">
        <button type="button" className="mtop__back" onClick={() => navigate(`/c/${conv.id}`)} title="回到入口（保留這段對話）">
          <Icon name="arrowLeft" strokeWidth={1.6} />
          <span>入口</span>
        </button>
        <div className="mtop__brand">
          <span className="mtop__name">{m.spaced}</span>
          <span className="mtop__en">{m.en}</span>
        </div>
        <p className="mtop__conv">{titleOf(conv)}</p>
        <div className="mtop__actions">
          <StatusChip className="mtop__status" />
          <button type="button" className="mtop__btn" onClick={() => setDrawer(true)} aria-label="對話紀錄">
            <Icon name="clock" strokeWidth={1.5} />
            <span>對話紀錄</span>
          </button>
          <button type="button" className="mtop__btn" onClick={() => navigate("/")} aria-label="新對話">
            <Icon name="newChat" strokeWidth={1.5} />
            <span>新對話</span>
          </button>
          <div className="topbar__slot">
            <button type="button" className={`mtop__btn${menu === "tools" ? " is-open" : ""}`} onClick={() => setMenu(menu === "tools" ? null : "tools")} aria-expanded={menu === "tools"}>
              <Icon name="grid" strokeWidth={1.5} />
              <span>功能頁</span>
            </button>
            <Popover open={menu === "tools"} onClose={close} className="menu--right">
              <p className="menu__label">完整功能頁（一樣經過後端的權限檢查）</p>
              {TOOLS[domain].map((t) => (
                <button
                  key={t.to}
                  type="button"
                  className="menu__item"
                  onClick={() => {
                    close();
                    navigate(t.to);
                  }}
                >
                  <span className="menu__text">{t.label}</span>
                  <Icon name="chevronRight" strokeWidth={2} />
                </button>
              ))}
            </Popover>
          </div>
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

      {narrow && (
        <div className="mtabs" role="tablist">
          <button type="button" role="tab" aria-selected={pane === "chat"} className={pane === "chat" ? "is-on" : ""} onClick={() => setPane("chat")}>
            對話
          </button>
          <button type="button" role="tab" aria-selected={pane === "show"} className={pane === "show" ? "is-on" : ""} onClick={() => setPane("show")}>
            展示區 <span className="num">{outs.length}</span>
            {activeOut && activeOut.key !== seen && pane === "chat" && <span className="mtabs__new">新</span>}
          </button>
        </div>
      )}

      <div ref={body} className="mbody" data-pane={narrow ? pane : undefined} style={{ "--split": `${shell.split}%` } as CSSProperties}>
        <section className="mchat" aria-label="對話">
          <header className="msubject">
            {subject ? (
              <>
                <p className="msubject__code">
                  {subject.id.toUpperCase()}・{subject.kind === "part" ? "圖紙" : "作品"}
                </p>
                <h1 className="msubject__title">{subject.label}</h1>
                <p className="msubject__by">{subject.meta}</p>
              </>
            ) : (
              <>
                <p className="msubject__code">{m.en}</p>
                <h1 className="msubject__title">{m.label}</h1>
                <p className="msubject__by">{m.pitch}</p>
              </>
            )}
          </header>
          <main ref={sticky.scroller} onScroll={sticky.onScroll} className="scroll">
            <div ref={sticky.content} className="thread">
              {conv.turns.length === 0 && <p className="mchat__hint">{m.pitch}。直接在下面輸入需求。</p>}
              <Thread
                conv={conv}
                actions={actions}
                afterOf={(t) => {
                  const d = domainOf(t);
                  if (!d || d === domain) return null;
                  const cta = ctaOf(t);
                  if (!cta) return null;
                  const keys = (outputs.byTurn[t.id] ?? []).filter((k) => outputs.list.find((o) => o.key === k)?.domain === cta.domain);
                  const first = keys.find((k) => /^(drawing|artwork):/.test(k)) ?? keys.at(-1);
                  return <JumpCard domain={cta.domain} line={cta.line} switching onGo={() => onJump(conv.id, cta.domain, first)} />;
                }}
              />
            </div>
          </main>
          <div className="dock">
            <div className="dock__inner">
              {conv.turns.length > 0 && !sticky.atBottom && (
                <button type="button" className="dock__down fade-in" onClick={sticky.toBottom} title="捲到最新" aria-label="捲到最新">
                  <Icon name="arrowDown" strokeWidth={2} />
                </button>
              )}
              <Composer busy={busy} copy={COPY[domain]} onSend={(t, img) => ask(t, img)} onStop={() => shell.stop(conv.id)} autoFocus />
              <p className="dock__status">LOCAL INFERENCE · EGRESS {egress ? `${(egress / 1024).toFixed(1)} KB → JEV` : "0 B"}</p>
            </div>
          </div>
        </section>

        {!narrow && (
          <div
            className="msplit"
            role="separator"
            aria-orientation="vertical"
            aria-valuenow={shell.split}
            aria-valuemin={30}
            aria-valuemax={70}
            aria-label="調整左右寬度（雙擊回到各一半）"
            tabIndex={0}
            onPointerDown={drag}
            onDoubleClick={() => shell.setSplit(50)}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft") shell.setSplit(shell.split - 2);
              if (e.key === "ArrowRight") shell.setSplit(shell.split + 2);
              if (e.key === "Home" || e.key === "Enter") shell.setSplit(50);
            }}
            title="拖曳調整寬度・雙擊回到各一半"
          >
            <span className="msplit__grip" />
          </div>
        )}

        <Showcase domain={domain} outputs={outs} active={activeOut} onSelect={select} onAsk={(q) => ask(q)} onRerun={(turnId) => shell.rerun(conv.id, turnId)} />
      </div>

      {drawer && (
        <div className="drawer" role="dialog" aria-modal="true" aria-label="對話紀錄">
          <div className="drawer__scrim" onClick={() => setDrawer(false)} />
          <Sidebar
            convs={shell.convs}
            activeId={conv.id}
            collapsed={false}
            variant="drawer"
            onToggle={() => {}}
            onNew={() => {
              setDrawer(false);
              navigate("/");
            }}
            onOpen={(c) => {
              setDrawer(false);
              navigate(c.module ? `/${c.module}/${c.id}` : `/c/${c.id}`);
            }}
            onDelete={(c) => {
              if (!window.confirm(`刪除這段對話紀錄？\n「${titleOf(c)}」\n\n只會刪掉這台電腦上的紀錄。`)) return;
              shell.remove(c.id);
              if (c.id === conv.id) navigate("/");
            }}
            onClose={() => setDrawer(false)}
          />
        </div>
      )}
    </div>
  );
}
