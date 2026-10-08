import { useMemo, useRef, useState } from "react";
import { MODULE } from "../../shell/design";
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

/**
 * 來源：ArtRAG-前端demo/source/src/components/Sidebar.tsx。
 * 對話紀錄：新對話、搜尋、依日期分組。入口左側可以收合成一條窄欄（只剩圖示與最近幾段），手機與模組裡以抽屜打開。
 */
export function Sidebar({
  convs,
  activeId,
  collapsed,
  variant,
  onToggle,
  onNew,
  onOpen,
  onDelete,
  onClose,
}: {
  convs: Conv[];
  activeId: string | null;
  collapsed: boolean;
  variant: "rail" | "drawer";
  onToggle: () => void;
  onNew: () => void;
  onOpen: (c: Conv) => void;
  onDelete: (c: Conv) => void;
  onClose?: () => void;
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

  if (mini)
    return (
      <aside className="side is-mini" aria-label="對話紀錄（已收合）">
        <button type="button" className="side__brand" onClick={onNew} title="ArtRAG・回到入口">
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
      </aside>
    );

  return (
    <aside className={`side${variant === "drawer" ? " side--drawer" : ""}`} aria-label="對話紀錄">
      <div className="side__head">
        <button type="button" className="side__brandrow" onClick={onNew} title="回到入口">
          <Mark />
          <span className="brand__name">ArtRAG</span>
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
      <p className="side__foot">紀錄存在這台電腦的瀏覽器裡，只保留公開資料；進過模組的對話，點了會直接回到模組看成果。</p>
    </aside>
  );
}
