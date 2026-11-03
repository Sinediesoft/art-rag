import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useAccounts, useArtwork, useArtworks } from "../api/hooks";
import type { SourceItem } from "../api/sse";
import { ArtDoc, citesFor } from "../components/art/ArtDoc";
import { ArtFiles } from "../components/art/ArtFiles";
import { AccountMenu, pendingCount, Popover, StatusChip, useTokenRenewal, WriteNotice, type Menu } from "../components/shell/Chrome";
import { Composer } from "../components/shell/Composer";
import { Icon, Mark } from "../components/shell/Icons";
import { JumpCard } from "../components/shell/Jump";
import { SettingsMenu, ThemeToggle } from "../components/shell/SettingsMenu";
import { downloadOf } from "../components/shell/Showcase";
import { Sidebar } from "../components/shell/Sidebar";
import { Thread } from "../components/shell/Thread";
import { SimilarView, TimelineView } from "../components/shell/Viewers";
import { COPY, MODULE, type Domain } from "../shell/design";
import { useEscape, useMedia, useStickyScroll } from "../shell/hooks";
import { artworkOf, ctaOf, deriveOutputs, domainOf, KIND_LABEL, subjectFor, withSubject, type Output, type Subject } from "../shell/outputs";
import { titleOf } from "../shell/persist";
import { egressOf, progressOf } from "../shell/stages";
import { useShell } from "../shell/store";
import type { Conv } from "../shell/types";
import { lockReason, useThreadActions } from "./shared";

/**
 * 藝術模組（mockups/painting-module.html，Obsidian 的版面配置、art-rag 的配色與元件）：
 * 最左模組列＋左邊對話＋中間作品文件（分頁）＋右邊資料夾。
 * - 分頁：這段對話產生的成果（和原本展示區的縮圖列同一份，跟著對話紀錄，不會消失），再加上從資料夾、其他候選打開的作品（可關掉）
 * - 作品文件：檢索到的圖片 → 畫作資料＋區域資料 → 色溫 → 影像對位與差異（components/art/）；年表、相似作品沿用原本的檢視
 * - 兩側可以收合、拖曳寬度（記在這個瀏覽器）；窄於 900px 兩側改成抽屜
 * 寫入一律不在這裡：建檔走功能頁，問答走左邊的對話（/agent/route 七段流程）。
 */
const DOMAIN: Domain = "art";

/** 兩側寬度與收合：只是這個瀏覽器的偏好，讀寫失敗（私密視窗、封鎖儲存）就用預設 */
const LAYOUT_KEY = "artrag-art-layout";
interface Layout {
  chat: number;
  files: number;
  noChat: boolean;
  noFiles: boolean;
}
const DEFAULT_LAYOUT: Layout = { chat: 400, files: 260, noChat: false, noFiles: false };
const CHAT_RANGE = [300, 640] as const;
const FILES_RANGE = [200, 420] as const;
const clamp = (v: number, [lo, hi]: readonly [number, number]) => Math.round(Math.min(hi, Math.max(lo, v)));
function loadLayout(): Layout {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "null") as Partial<Layout> | null;
    if (!raw || typeof raw !== "object") return DEFAULT_LAYOUT;
    const num = (v: unknown, d: number, r: readonly [number, number]) => (typeof v === "number" && Number.isFinite(v) ? clamp(v, r) : d);
    return { chat: num(raw.chat, DEFAULT_LAYOUT.chat, CHAT_RANGE), files: num(raw.files, DEFAULT_LAYOUT.files, FILES_RANGE), noChat: raw.noChat === true, noFiles: raw.noFiles === true };
  } catch {
    return DEFAULT_LAYOUT;
  }
}
function saveLayout(L: Layout) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(L));
  } catch {
    // 存不了就只在這次有效
  }
}

const isDoc = (o: Output) => (o.kind === "artwork" || o.kind === "detail") && !!o.artworkId;
/** 空白時的建議問句：用知識庫裡真的有的第一幅畫（送進對話，經 /agent/route 分派） */
const asksOf = (t: string | undefined) =>
  t ? [`介紹〈${t}〉`, `細看〈${t}〉的技法`, `看看〈${t}〉的年表`, `有沒有和〈${t}〉風格相近的作品？`] : ["介紹一幅館藏的畫", "有哪些印象派的作品？"];

export function ArtModuleView({ conv, onJump }: { conv: Conv; onJump: (convId: string, domain: Domain, key?: string) => void }) {
  const shell = useShell();
  const navigate = useNavigate();
  const m = MODULE[DOMAIN];
  const narrow = !useMedia("(min-width: 900px)");
  const { data: accounts } = useAccounts();
  const { data: kb } = useArtworks();
  const renewal = useTokenRenewal();
  const [menu, setMenu] = useState<Menu>(null);
  const close = () => setMenu(null);
  const [drawer, setDrawer] = useState(false);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  useEscape(drawer, closeDrawer);

  // ---------------------------------------------------------------- 分頁：對話的成果＋從資料夾打開的作品
  const outputs = deriveOutputs(conv);
  const outs = outputs.list.filter((o) => o.domain === DOMAIN);
  const [browse, setBrowse] = useState<string[]>([]);
  const labelOf = (id: string) => kb?.items.find((a) => a.id === id)?.title_zh ?? id;
  const browseOuts: Output[] = browse.map((id) => ({ key: `browse:${id}`, kind: "artwork", domain: DOMAIN, turnId: "", artworkId: id, title: labelOf(id), meta: "知識庫・從資料夾打開" }));
  const tabs = [...outs, ...browseOuts];
  const active = tabs.find((o) => o.key === conv.active[DOMAIN]) ?? outs.at(-1) ?? browseOuts.at(-1);
  const idx = active ? tabs.findIndex((o) => o.key === active.key) : -1;
  const { data: art } = useArtwork(active?.artworkId);
  const [focus, setFocus] = useState<{ key: string; chunk: string; seq: number } | null>(null);
  const [openChat, setOpenChat] = useState(narrow);
  const [openFiles, setOpenFiles] = useState(false);
  const select = (key: string) => {
    setFocus(null);
    shell.setActive(conv.id, DOMAIN, key);
  };
  /** 打開一幅畫：對話裡已經有這幅畫（沒附照片的那份）就切過去，沒有就開一個分頁 */
  const open = (id: string) => {
    const has = outs.find((o) => o.kind === "artwork" && o.artworkId === id && !o.imageId) ?? outs.find((o) => isDoc(o) && o.artworkId === id);
    if (has) return select(has.key);
    setBrowse((b) => (b.includes(id) ? b : [...b, id]));
    select(`browse:${id}`);
    if (narrow) setOpenFiles(false);
  };
  const subject: Subject | null = active?.key.startsWith("browse:") ? { kind: "artwork", id: active.artworkId!, label: active.title, meta: active.meta } : subjectFor(DOMAIN, active, conv);
  const mentioned = useMemo(() => {
    const seen = new Map<string, string>();
    for (const t of conv.turns) {
      const a = artworkOf(t);
      if (a && !seen.has(a.id)) seen.set(a.id, a.label);
    }
    return [...seen].map(([id, label]) => ({ id, label }));
  }, [conv.turns]);

  // ---------------------------------------------------------------- 對話
  const sticky = useStickyScroll(conv.id, true);
  const busy = conv.turns.some((t) => t.phase === "routing" || t.phase === "running");
  /** 說「這幅畫」時帶入正在看的作品（後端一句一句判斷，沒有上下文） */
  const ask = (text: string, imageId: string | null = null, forced: string | null = null) => {
    shell.ask(conv.id, DOMAIN, { text: withSubject(text, subject), imageId, forced });
    sticky.follow();
    if (narrow) setOpenChat(true);
  };
  const base = useThreadActions(conv, "full", (q, forced, imageId) => ask(q, imageId ?? null, forced ?? null), DOMAIN);
  const seq = useRef(0);
  /** 回答裡點了引用 [n]：中間切到那幅畫，捲到那一段 */
  const onCite = (s: SourceItem) => {
    if (!s.artwork_id) return;
    let key = active?.artworkId === s.artwork_id && active && isDoc(active) ? active.key : undefined;
    if (!key) {
      const has = outs.find((o) => isDoc(o) && o.artworkId === s.artwork_id);
      if (has) key = has.key;
      else {
        setBrowse((b) => (b.includes(s.artwork_id!) ? b : [...b, s.artwork_id!]));
        key = `browse:${s.artwork_id}`;
      }
      shell.setActive(conv.id, DOMAIN, key);
    }
    setFocus({ key, chunk: s.chunk_id, seq: ++seq.current });
    if (narrow) setOpenChat(false);
  };
  const actions = { ...base, onCite };
  const last = conv.turns.at(-1);
  const egress = last?.route ? egressOf(last.route, progressOf(last.part)).bytes : 0;
  const recent = useMemo(
    () =>
      shell.convs
        .filter((c) => c.id === conv.id || (c.module === DOMAIN && c.turns.length > 0))
        .sort((a, b) => b.updatedAt - a.updatedAt)
        .slice(0, 4)
        .sort((a, b) => a.createdAt - b.createdAt),
    [shell.convs, conv.id],
  );
  const newConv = () => {
    const id = shell.openModule(DOMAIN);
    if (id !== conv.id) navigate(`/art/${id}`);
  };

  // ---------------------------------------------------------------- 版面：兩側收合、拖曳寬度
  const [L, setL] = useState<Layout>(loadLayout);
  const setLayout = (patch: Partial<Layout>) =>
    setL((l) => {
      const next = { ...l, ...patch };
      saveLayout(next);
      return next;
    });
  const toggleChat = () => (narrow ? (setOpenChat((x) => !x), setOpenFiles(false)) : setLayout({ noChat: !L.noChat }));
  const toggleFiles = () => (narrow ? (setOpenFiles((x) => !x), setOpenChat(false)) : setLayout({ noFiles: !L.noFiles }));
  const resize = (side: "chat" | "files") => (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    el.setPointerCapture?.(e.pointerId);
    const x0 = e.clientX;
    const w0 = side === "chat" ? L.chat : L.files;
    el.classList.add("is-dragging");
    document.body.classList.add("is-dragging");
    const move = (ev: PointerEvent) => {
      const d = ev.clientX - x0;
      setLayout(side === "chat" ? { chat: clamp(w0 + d, CHAT_RANGE) } : { files: clamp(w0 - d, FILES_RANGE) });
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      el.classList.remove("is-dragging");
      document.body.classList.remove("is-dragging");
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const work = useRef<HTMLElement>(null);
  const [full, setFull] = useState(false);
  useEffect(() => {
    const h = () => setFull(document.fullscreenElement === work.current);
    document.addEventListener("fullscreenchange", h);
    return () => document.removeEventListener("fullscreenchange", h);
  }, []);
  const view = useRef<HTMLDivElement>(null);
  useEffect(() => {
    view.current?.scrollTo?.({ top: 0 });
  }, [active?.key]);

  const dl = active ? downloadOf(active) : null;
  const doc = active && isDoc(active);
  const crumbs = ["知識庫", "畫作", ...(art?.artist.zh ? [art.artist.zh] : []), ...(active && !doc ? [KIND_LABEL[active.kind]] : [])];
  const style = { "--aw-chat": `${L.noChat ? 0 : L.chat}px`, "--aw-files": `${L.noFiles ? 0 : L.files}px` } as CSSProperties;

  return (
    <div
      className={["app", "app--art", "aw", L.noChat && !narrow ? "no-chat" : "", L.noFiles && !narrow ? "no-files" : "", openChat && narrow ? "open-chat" : "", openFiles && narrow ? "open-files" : ""].filter(Boolean).join(" ")}
      style={style}
    >
      {/* ---------------- 最左：模組列 ---------------- */}
      <nav className="aw-ribbon" aria-label="模組">
        <Mark className="aw-ribbon__mark" />
        <button type="button" className="aw-icon-btn" onClick={() => navigate(conv.turns.length ? `/c/${conv.id}` : "/")} title={conv.turns.length ? "回到入口（保留這段對話）" : "回到入口"} aria-label="回入口">
          <Icon name="home" />
        </button>
        <button type="button" className="aw-icon-btn is-on" title="畫作" aria-label="畫作" aria-current="page" onClick={() => view.current?.scrollTo?.({ top: 0, behavior: "smooth" })}>
          <Icon name="frame" />
        </button>
      </nav>

      {/* ---------------- 左：對話 ---------------- */}
      <aside className="aw-pane aw-chat mchat" aria-label="對話" inert={(narrow && !openChat) || (!narrow && L.noChat) || undefined}>
        <div className="aw-pane__head">
          <p className="aw-brand">
            <span className="aw-brand__name">{m.label}</span>
            <span className="aw-en">{m.en}</span>
          </p>
          <button type="button" className="aw-icon-btn" onClick={newConv} title="新對話（藝術模組）" aria-label="新對話">
            <Icon name="newChat" />
          </button>
          <button type="button" className="aw-icon-btn" onClick={() => setDrawer(true)} title="對話紀錄" aria-label="對話紀錄">
            <Icon name="clock" />
          </button>
          {narrow && (
            <button type="button" className="aw-icon-btn" onClick={() => setOpenChat(false)} title="收起對話" aria-label="收起對話">
              <Icon name="x" />
            </button>
          )}
        </div>
        <main ref={sticky.scroller} onScroll={sticky.onScroll} className="scroll">
          <div ref={sticky.content} className="thread">
            {conv.turns.length === 0 && <p className="mchat__hint">{m.pitch}。附上一張照片，可以查是館藏的哪一件、和原圖哪裡不一樣。</p>}
            <Thread
              conv={conv}
              actions={actions}
              afterOf={(t) => {
                const d = domainOf(t);
                if (!d || d === DOMAIN) return null;
                const cta = ctaOf(t);
                if (!cta) return null;
                const keys = (outputs.byTurn[t.id] ?? []).filter((k) => outputs.list.find((o) => o.key === k)?.domain === cta.domain);
                const first = keys.find((k) => /^(drawing|artwork):/.test(k)) ?? keys.at(-1);
                return <JumpCard domain={cta.domain} line={cta.line} switching onGo={() => onJump(conv.id, cta.domain, first)} />;
              }}
            />
          </div>
        </main>
        <div className="aw-sessions" role="tablist" aria-label="最近的藝術模組對話">
          {recent.map((c, i) => (
            <button key={c.id} type="button" role="tab" aria-selected={c.id === conv.id} className={`aw-sess num${c.id === conv.id ? " is-on" : ""}`} onClick={() => c.id !== conv.id && navigate(`/art/${c.id}`)} title={titleOf(c)}>
              {i + 1}
            </button>
          ))}
          <span className="aw-spacer" />
          <button type="button" className="aw-icon-btn" onClick={newConv} title="新對話（藝術模組）" aria-label="新對話分頁">
            <Icon name="plus" />
          </button>
        </div>
        <div className="dock">
          <div className="dock__inner">
            {conv.turns.length > 0 && !sticky.atBottom && (
              <button type="button" className="dock__down fade-in" onClick={sticky.toBottom} title="捲到最新" aria-label="捲到最新">
                <Icon name="arrowDown" strokeWidth={2} />
              </button>
            )}
            <Composer
              busy={busy}
              copy={COPY[DOMAIN]}
              onSend={(t, img) => ask(t, img)}
              onStop={() => shell.stop(conv.id)}
              autoFocus={!narrow}
              locked={lockReason(shell)}
              resetKey={shell.accountId}
              extra={
                <button type="button" className="composer__attach composer__extra" onClick={() => navigate("/artworks/intake")} title="照片建檔：把知識庫還沒有的畫作建進去（主管收錄後生效）">
                  <span className="composer__attach-label">圖片建檔</span>
                </button>
              }
            />
          </div>
        </div>
      </aside>

      {!narrow && !L.noChat && (
        <div
          className="aw-resizer aw-resizer--chat"
          role="separator"
          aria-orientation="vertical"
          aria-label="調整對話欄寬度（雙擊還原）"
          aria-valuenow={L.chat}
          aria-valuemin={CHAT_RANGE[0]}
          aria-valuemax={CHAT_RANGE[1]}
          tabIndex={0}
          onPointerDown={resize("chat")}
          onDoubleClick={() => setLayout({ chat: DEFAULT_LAYOUT.chat })}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft" || e.key === "ArrowRight") setLayout({ chat: clamp(L.chat + (e.key === "ArrowRight" ? 20 : -20), CHAT_RANGE) });
          }}
        />
      )}

      {/* ---------------- 中：作品文件 ---------------- */}
      <main ref={work} className="aw-work" aria-label="作品">
        <div className="aw-tabbar">
          <button type="button" className={`aw-icon-btn${(narrow ? openChat : !L.noChat) ? "" : " is-on"}`} onClick={toggleChat} title={narrow ? "對話" : L.noChat ? "展開對話" : "收合對話"} aria-label={narrow ? "對話" : L.noChat ? "展開對話" : "收合對話"}>
            <Icon name="sidebar" />
          </button>
          <span className="aw-spacer" />
          <ThemeToggle className="aw-icon-btn" />
          <div className="topbar__slot">
            <button type="button" className={`aw-icon-btn${menu === "settings" ? " is-on" : ""}`} onClick={() => setMenu(menu === "settings" ? null : "settings")} aria-expanded={menu === "settings"} title="設定" aria-label="設定">
              <Icon name="settings" />
            </button>
            <Popover open={menu === "settings"} onClose={close} className="menu--right menu--settings">
              <SettingsMenu />
            </Popover>
          </div>
          <button type="button" className={`aw-icon-btn${(narrow ? openFiles : !L.noFiles) ? "" : " is-on"}`} onClick={toggleFiles} title={narrow ? "資料夾" : L.noFiles ? "展開資料夾" : "收合資料夾"} aria-label={narrow ? "資料夾" : L.noFiles ? "展開資料夾" : "收合資料夾"}>
            <Icon name="sidebarRight" />
          </button>
        </div>
        <WriteNotice />
        {renewal.renewed && (
          <p className="banner" role="status">
            {renewal.renewed}：已重新取得〈{renewal.label}〉的憑證。
            <button type="button" className="link-btn" onClick={renewal.dismiss}>
              知道了
            </button>
          </p>
        )}

        <div className="aw-viewhead">
          <button type="button" className="aw-icon-btn" onClick={() => idx > 0 && select(tabs[idx - 1].key)} disabled={idx <= 0} title="上一個分頁" aria-label="上一個分頁">
            <Icon name="arrowLeft" />
          </button>
          <button type="button" className="aw-icon-btn" onClick={() => idx < tabs.length - 1 && select(tabs[idx + 1].key)} disabled={idx < 0 || idx >= tabs.length - 1} title="下一個分頁" aria-label="下一個分頁">
            <Icon name="arrowRight" />
          </button>
          <p className="aw-crumbs">
            {crumbs.join(" / ")}
            {active ? (
              <>
                {" / "}
                <b>{active.kind === "similar" && active.search ? `「${active.search}」` : (art?.title.zh ?? subject?.label ?? active.title)}</b>
              </>
            ) : null}
          </p>
          {dl && (
            <button type="button" className="aw-icon-btn" onClick={dl} title="下載原圖" aria-label="下載原圖">
              <Icon name="download" />
            </button>
          )}
          <button type="button" className="aw-icon-btn" onClick={() => (full ? void document.exitFullscreen() : void work.current?.requestFullscreen?.())} title={full ? "離開全螢幕" : "全螢幕"} aria-label={full ? "離開全螢幕" : "全螢幕"}>
            <Icon name={full ? "unfocus" : "expand"} />
          </button>
        </div>

        {active && doc ? (
          <div ref={view} className="aw-view" key={active.key}>
            <ArtDoc
              o={active}
              when={conv.turns.find((t) => t.id === active.turnId)?.ts}
              cites={citesFor(active.artworkId!, conv.turns)}
              focus={focus?.key === active.key ? focus : null}
              onOpen={open}
              onAsk={ask}
            />
          </div>
        ) : active ? (
          <div className="aw-legacy" key={active.key}>
            <div className="showcase__body">
              {active.kind === "timeline" && <TimelineView o={active} onAsk={ask} />}
              {active.kind === "similar" && <SimilarView o={active} onAsk={ask} />}
            </div>
          </div>
        ) : (
          <div ref={view} className="aw-view">
            <div className="aw-doc aw-empty-doc">
              <p className="aw-subject__code">{m.en}</p>
              <h1 className="aw-subject__title">{m.label}</h1>
              <p className="aw-subject__by">對話產生的作品會在這裡展開成一份文件：檢索到的圖片、畫作資料、色溫，附上照片時再加上影像對位與差異。</p>
              <p className="aw-empty-doc__hint">在左邊提問，或從右邊的資料夾直接打開一幅畫。也可以試試：</p>
              <div className="aw-empty-doc__asks">
                {asksOf(kb?.items[0]?.title_zh).map((q) => (
                  <button key={q} type="button" className="aw-tool" onClick={() => ask(q)} disabled={busy || !!lockReason(shell)}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}

        <div className="aw-statusbar">
          <StatusChip className="aw-statusbar__chip" />
          <span className="num">知識庫 {kb ? kb.items.length.toLocaleString() : "—"} 件</span>
          <span className="num">外送 {egress ? `${(egress / 1024).toFixed(1)} KB → JEV` : "0 BYTES"}</span>
          <div className="topbar__slot">
            <button type="button" className={`aw-statusbar__account${menu === "account" ? " is-open" : ""}`} onClick={() => setMenu(menu === "account" ? null : "account")} aria-expanded={menu === "account"} title="切換身分">
              <Icon name="user" />
              <span>{accounts?.current.label ?? "讀取中"}</span>
              {pendingCount(accounts) > 0 && <span className="count">{pendingCount(accounts)}</span>}
            </button>
            <Popover open={menu === "account"} onClose={close} className="menu--right menu--up">
              <AccountMenu onClose={close} />
            </Popover>
          </div>
        </div>
      </main>

      {!narrow && !L.noFiles && (
        <div
          className="aw-resizer aw-resizer--files"
          role="separator"
          aria-orientation="vertical"
          aria-label="調整資料夾寬度（雙擊還原）"
          aria-valuenow={L.files}
          aria-valuemin={FILES_RANGE[0]}
          aria-valuemax={FILES_RANGE[1]}
          tabIndex={0}
          onPointerDown={resize("files")}
          onDoubleClick={() => setLayout({ files: DEFAULT_LAYOUT.files })}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft" || e.key === "ArrowRight") setLayout({ files: clamp(L.files + (e.key === "ArrowLeft" ? 20 : -20), FILES_RANGE) });
          }}
        />
      )}

      {/* ---------------- 右：資料夾 ---------------- */}
      <aside className="aw-pane aw-files" aria-label="資料夾" inert={(narrow && !openFiles) || (!narrow && L.noFiles) || undefined}>
        <ArtFiles currentId={active?.artworkId ?? null} mentioned={mentioned} onOpen={open} />
      </aside>

      {narrow && (openChat || openFiles) && (
        <div
          className="aw-scrim"
          onClick={() => {
            setOpenChat(false);
            setOpenFiles(false);
          }}
        />
      )}

      {drawer && (
        <div className="drawer" role="dialog" aria-modal="true" aria-label="對話紀錄">
          <div className="drawer__scrim" onClick={() => setDrawer(false)} />
          <Sidebar
            convs={shell.convs}
            activeId={conv.id}
            collapsed={false}
            variant="drawer"
            current={DOMAIN}
            onToggle={() => {}}
            onNew={() => {
              setDrawer(false);
              navigate("/");
            }}
            onModule={(d) => {
              setDrawer(false);
              if (d === DOMAIN && conv.turns.length === 0) return;
              onJump(shell.openModule(d), d);
            }}
            onTool={(to) => {
              setDrawer(false);
              navigate(to);
            }}
            onOpen={(c) => {
              setDrawer(false);
              navigate(c.module ? `/${c.module}/${c.id}` : `/c/${c.id}`);
            }}
            onDelete={(c) => {
              if (!window.confirm(`刪除這段對話紀錄？\n「${titleOf(c)}」\n\n只會刪掉這台電腦上的紀錄。`)) return;
              if (!shell.remove(c.id)) return window.alert("有寫入還沒收到結果，等結果回來再刪除這段對話。");
              if (c.id === conv.id) navigate("/");
            }}
            onClose={() => setDrawer(false)}
          />
        </div>
      )}
    </div>
  );
}
