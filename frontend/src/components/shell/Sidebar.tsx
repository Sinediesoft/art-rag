import { useMemo, useRef, useState, type ReactNode } from "react";
import { useCanView } from "../../api/hooks";
import { MODULE, TOOLS, type Domain } from "../../shell/design";
import { domainOf } from "../../shell/outputs";
import { timeOf, titleOf, whenOf } from "../../shell/persist";
import type { Conv } from "../../shell/types";
import { Icon, Mark } from "./Icons";

/** 一段紀錄的摘要：第一句被分派成什麼、屬於哪個模組（只看已經回來的結果，不重新呼叫） */
function infoOf(c: Conv) {
  const t = c.turns[0];
  const label = t?.route?.intent_label ?? t?.archived?.intentLabel ?? "對話";
  const domain = c.module ?? c.turns.map(domainOf).find(Boolean) ?? null;
  return { label, domain, inModule: !!c.module };
}

const DOMAINS: Domain[] = ["factory", "art"];
const MOD_ICON: Record<Domain, string> = { factory: "factory", art: "palette" };
/**
 * 開發者平台是 public/ 裡的獨立 HTML、不是路由：用 <a> 另開分頁（navigate 會被 * 導回首頁，同一分頁開也會換掉這裡的對話）。
 * 它的診斷、稽核、評估只開給 access.yaml 的 views.diagnostics（預設主管），入口跟著同一個條件
 */
const DEV_URL = "/developer-workbench.html";

/**
 * 模組捷徑：按名稱直接進模組（不用先問一句），按右邊的箭頭展開那個模組的功能頁、點了直接開。
 * 一次只展開一個模組，清單太長時自己捲動，不會把對話紀錄擠掉
 */
function ModuleNav({ current, onModule, onTool }: { current?: Domain; onModule: (d: Domain) => void; onTool: (to: string) => void }) {
  const [open, setOpen] = useState<Domain | null>(null);
  const canDev = useCanView("diagnostics");
  return (
    <nav className="side__mods" aria-label="模組">
      <p className="side__label">
        <span>模組</span>
      </p>
      {DOMAINS.map((d) => {
        const m = MODULE[d];
        const isOpen = open === d;
        return (
          <div key={d} className={`side__modgroup is-${d}${isOpen ? " is-open" : ""}${current === d ? " is-on" : ""}`}>
            <div className="side__modrow">
              <button type="button" className="side__modgo" onClick={() => onModule(d)} title={`進入${m.label}：${m.pitch}`}>
                <span className="side__modicon">
                  <Icon name={MOD_ICON[d]} strokeWidth={1.5} />
                </span>
                <span className="side__modtext">
                  <span className="side__modname">{m.label}</span>
                  <span className="side__modsub">{m.asks.join("・")}</span>
                </span>
              </button>
              <button
                type="button"
                className="side__modtoggle"
                onClick={() => setOpen(isOpen ? null : d)}
                aria-expanded={isOpen}
                aria-controls={`side-tools-${d}`}
                title={isOpen ? "收起功能" : `${m.label}的功能`}
                aria-label={`${isOpen ? "收起" : "展開"}${m.label}的功能`}
              >
                <Icon name="chevronDown" strokeWidth={2} />
              </button>
            </div>
            {isOpen && (
              <ul className="side__tools fade-in" id={`side-tools-${d}`}>
                {TOOLS[d].map((t) => (
                  <li key={t.to}>
                    <button type="button" className="side__tool" onClick={() => onTool(t.to)}>
                      <Icon name={t.icon} />
                      <span>{t.label}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
      {canDev && (
        <div className="side__modgroup">
          <div className="side__modrow">
            <a className="side__modgo" href={DEV_URL} target="_blank" rel="noopener" title="另開分頁：開發者平台（系統診斷、請求追蹤、檢索除錯、資料流）">
              <span className="side__modicon">
                <Icon name="code" strokeWidth={1.5} />
              </span>
              <span className="side__modtext">
                <span className="side__modname">開發者平台</span>
                <span className="side__modsub">診斷・追蹤・檢索・資料流</span>
              </span>
              <span className="side__modext" aria-hidden="true">
                ↗
              </span>
            </a>
          </div>
        </div>
      )}
    </nav>
  );
}

/**
 * 來源：ArtRAG-前端demo/source/src/components/Sidebar.tsx。
 * 對話紀錄：新對話、模組捷徑、搜尋、依日期分組。入口左側可以收合成一條窄欄（只剩圖示與最近幾段），手機與模組裡以抽屜打開。
 * 有 foot 時（入口：目前身分）放在最下面，取代說明文字；收合成窄欄時也留在最下面。
 */
export function Sidebar({
  convs,
  activeId,
  collapsed,
  variant,
  current,
  onToggle,
  onNew,
  onModule,
  onTool,
  onOpen,
  onDelete,
  onClose,
  foot,
}: {
  convs: Conv[];
  activeId: string | null;
  collapsed: boolean;
  variant: "rail" | "drawer";
  /** 目前所在的模組（模組裡打開的抽屜）：捷徑標示「在這裡」 */
  current?: Domain;
  onToggle: () => void;
  onNew: () => void;
  onModule: (d: Domain) => void;
  onTool: (to: string) => void;
  onOpen: (c: Conv) => void;
  onDelete: (c: Conv) => void;
  onClose?: () => void;
  foot?: ReactNode;
}) {
  const [q, setQ] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const sorted = useMemo(() => convs.filter((c) => c.turns.length > 0).sort((a, b) => b.updatedAt - a.updatedAt), [convs]);
  const infos = useMemo(() => new Map(sorted.map((c) => [c.id, infoOf(c)])), [sorted]);
  const needle = q.trim();
  const shown = needle
    ? sorted.filter((c) => c.turns.some((t) => (t.route?.question || t.text).includes(needle)) || infos.get(c.id)!.label.includes(needle))
    : sorted;
  const groups: [string, Conv[]][] = [];
  for (const c of shown) {
    const g = whenOf(c.updatedAt);
    const last = groups[groups.length - 1];
    if (last && last[0] === g) last[1].push(c);
    else groups.push([g, [c]]);
  }
  const mini = collapsed && variant === "rail";
  const canDev = useCanView("diagnostics");

  if (mini)
    return (
      <aside className="side is-mini" aria-label="對話紀錄（已收合）">
        <button type="button" className="side__brand" onClick={onNew} title="Multimodal-RAG・回到入口">
          <Mark />
        </button>
        <button type="button" className="side__icon" onClick={onToggle} title="展開對話紀錄" aria-label="展開對話紀錄">
          <Icon name="sidebar" />
        </button>
        <button type="button" className="side__icon" onClick={onNew} title="新對話" aria-label="新對話">
          <Icon name="newChat" />
        </button>
        <button
          type="button"
          className="side__icon"
          onClick={() => {
            onToggle();
            setTimeout(() => search.current?.focus(), 60);
          }}
          title="搜尋對話紀錄"
          aria-label="搜尋對話紀錄"
        >
          <Icon name="search" />
        </button>
        <span className="side__rule" />
        {DOMAINS.map((d) => (
          <button key={d} type="button" className={`side__icon side__modmini is-${d}`} onClick={() => onModule(d)} title={`進入${MODULE[d].label}（展開側欄可以直接選功能）`} aria-label={`進入${MODULE[d].label}`}>
            <Icon name={MOD_ICON[d]} strokeWidth={1.5} />
          </button>
        ))}
        {canDev && (
          <a className="side__icon" href={DEV_URL} target="_blank" rel="noopener" title="另開分頁：開發者平台" aria-label="開發者平台（另開分頁）">
            <Icon name="code" strokeWidth={1.5} />
          </a>
        )}
        <span className="side__rule" />
        <ol className="side__minis">
          {sorted.slice(0, 8).map((c) => {
            const info = infos.get(c.id)!;
            return (
              <li key={c.id}>
                <button
                  type="button"
                  className={`side__mini${c.id === activeId ? " is-on" : ""}${info.domain ? ` is-${info.domain}` : ""}`}
                  onClick={() => onOpen(c)}
                  title={`${info.label}・${titleOf(c)}`}
                >
                  {titleOf(c).slice(0, 1)}
                </button>
              </li>
            );
          })}
        </ol>
        {foot && <div className="side__status">{foot}</div>}
      </aside>
    );

  return (
    <aside className={`side${variant === "drawer" ? " side--drawer" : ""}`} aria-label="對話紀錄">
      <div className="side__head">
        <button type="button" className="side__brandrow" onClick={onNew} title="回到入口">
          <Mark />
          <span className="brand__name">Multimodal-RAG</span>
        </button>
        {variant === "rail" ? (
          <button type="button" className="side__icon" onClick={onToggle} title="收合對話紀錄" aria-label="收合對話紀錄">
            <Icon name="sidebar" />
          </button>
        ) : (
          <button type="button" className="side__icon" onClick={onClose} title="關閉" aria-label="關閉對話紀錄">
            <Icon name="x" />
          </button>
        )}
      </div>

      <button type="button" className="side__new" onClick={onNew}>
        <Icon name="newChat" />
        <span>新對話</span>
      </button>

      <ModuleNav current={current} onModule={onModule} onTool={onTool} />

      <label className="side__search">
        <Icon name="search" />
        <input ref={search} value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜尋對話紀錄" aria-label="搜尋對話紀錄" />
        {q && (
          <button type="button" onClick={() => setQ("")} title="清除" aria-label="清除搜尋">
            <Icon name="x" strokeWidth={2} />
          </button>
        )}
      </label>

      <nav className="side__list" aria-label="對話紀錄清單">
        <p className="side__label">
          <span>對話紀錄</span>
          <span className="num">{shown.length}</span>
        </p>
        {groups.map(([g, list]) => (
          <section key={g} className="side__group">
            <p className="side__when">{g}</p>
            <ol>
              {list.map((c) => {
                const info = infos.get(c.id)!;
                return (
                  <li key={c.id} className={`side__item${c.id === activeId ? " is-on" : ""}`}>
                    <button type="button" className="side__open" onClick={() => onOpen(c)}>
                      <span className="side__meta">
                        <span className={`side__dot${info.domain ? ` is-${info.domain}` : ""}`} />
                        {info.label}
                        {info.inModule && info.domain && <span className="side__mod">{MODULE[info.domain].label}</span>}
                        <span className="side__time num">{timeOf(c.updatedAt)}</span>
                      </span>
                      <span className="side__title">{titleOf(c)}</span>
                      {c.turns.length > 1 && <span className="side__more">還有 {c.turns.length - 1} 則追問</span>}
                    </button>
                    <button type="button" className="side__del" onClick={() => onDelete(c)} title="刪除這段紀錄" aria-label={`刪除「${titleOf(c)}」`}>
                      <Icon name="trash" />
                    </button>
                  </li>
                );
              })}
            </ol>
          </section>
        ))}
        {!shown.length && <p className="side__empty">{q ? "找不到符合的對話。" : "還沒有對話紀錄。"}</p>}
      </nav>
      {foot ? (
        <div className="side__foot side__status">{foot}</div>
      ) : (
        <p className="side__foot">紀錄存在這台電腦的瀏覽器裡，只保留公開資料；進過模組的對話，點了會直接回到模組看成果。</p>
      )}
    </aside>
  );
}
