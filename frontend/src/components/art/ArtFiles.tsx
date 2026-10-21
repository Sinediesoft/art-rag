import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ArtworkSummary } from "../../api/client";
import { useArtworks } from "../../api/hooks";
import { Icon, Mark } from "../shell/Icons";

/**
 * 藝術模組右邊的資料夾（mockups/painting-module.html）：知識庫的畫作依畫家分組（GET /artworks，公開資料）。
 * 點一幅畫在中間開成分頁，只讀知識庫、不經過生成；上方三顆切換：資料夾、搜尋、這段對話談到的作品。
 */
type View = "tree" | "search" | "mentioned";

const short = (s: string) => s.split("（")[0];
const yearOf = (s: string) => Number(s.match(/(\d{3,4})/)?.[1] ?? 9999);

export function ArtFiles({
  currentId,
  mentioned,
  searchSeq,
  onOpen,
}: {
  /** 中間正在看的畫作 */
  currentId: string | null;
  /** 這段對話談到的畫作（依出現順序） */
  mentioned: { id: string; label: string }[];
  /** 分頁列的「＋」：切到搜尋並把游標放進搜尋框（數字變了就做一次） */
  searchSeq: number;
  onOpen: (id: string) => void;
}) {
  const { data, error, isLoading } = useArtworks();
  const [view, setView] = useState<View>("tree");
  const [q, setQ] = useState("");
  const [byYear, setByYear] = useState(false);
  const [closed, setClosed] = useState<Set<string>>(() => new Set());
  const [opened, setOpened] = useState<Set<string>>(() => new Set());
  const input = useRef<HTMLInputElement>(null);
  const items = useMemo(() => data?.items ?? [], [data]);

  useEffect(() => {
    if (!searchSeq) return;
    setView("search");
    window.setTimeout(() => input.current?.focus(), 0);
  }, [searchSeq]);

  const groups = useMemo(() => {
    const m = new Map<string, ArtworkSummary[]>();
    for (const a of items) m.set(a.artist_zh, [...(m.get(a.artist_zh) ?? []), a]);
    const sortWorks = (xs: ArtworkSummary[]) => [...xs].sort((x, y) => (byYear ? yearOf(x.date_text) - yearOf(y.date_text) : x.title_zh.localeCompare(y.title_zh, "zh-Hant")));
    return [...m.entries()].sort(([x], [y]) => x.localeCompare(y, "zh-Hant")).map(([artist, works]) => ({ artist, works: sortWorks(works) }));
  }, [items, byYear]);

  const current = items.find((a) => a.id === currentId);
  // 畫家資料夾預設收起；中間換到哪幅畫，就把那位畫家展開（之後照使用者自己的開關，不會因為換畫又收起來）
  useEffect(() => {
    if (!current) return;
    const key = `artist:${current.artist_zh}`;
    setOpened((s) => (s.has(key) ? s : new Set(s).add(key)));
    setClosed((s) => {
      if (!s.has(key) && !s.has("kb") && !s.has("art")) return s;
      const n = new Set(s);
      n.delete(key);
      n.delete("kb");
      n.delete("art");
      return n;
    });
  }, [current?.artist_zh]); // eslint-disable-line react-hooks/exhaustive-deps
  const isOpen = (key: string) => (closed.has(key) ? false : opened.has(key) || key === "kb" || key === "art");
  const toggle = (key: string) => {
    const open = isOpen(key);
    setClosed((s) => {
      const n = new Set(s);
      if (open) n.add(key);
      else n.delete(key);
      return n;
    });
    setOpened((s) => {
      const n = new Set(s);
      if (open) n.delete(key);
      else n.add(key);
      return n;
    });
  };
  const collapseAll = () => {
    setOpened(new Set());
    setClosed(new Set(["kb", "art", ...groups.map((g) => `artist:${g.artist}`)]));
  };

  const query = q.trim().toLowerCase();
  const found = query
    ? items.filter((a) => [a.title_zh, a.title_en ?? "", a.artist_zh, a.artist_en ?? "", ...a.style_tags].some((s) => s.toLowerCase().includes(query)))
    : [];

  const file = (a: ArtworkSummary, depth = 0) => (
    <li key={a.id}>
      <button type="button" className={`aw-tree__row${a.id === currentId ? " is-on" : ""}`} style={depth ? undefined : { paddingLeft: 8 }} onClick={() => onOpen(a.id)} title={`〈${a.title_zh}〉${a.artist_zh}・${short(a.date_text)}`}>
        <Icon name="image" />
        <span className="aw-tree__lbl">{a.title_zh}</span>
        <span className="aw-tree__ext">{short(a.date_text)}</span>
      </button>
    </li>
  );
  const folder = (key: string, label: string, count: number | null, children: ReactNode) => (
    <li key={key} className={isOpen(key) ? "" : "is-closed"}>
      <button type="button" className="aw-tree__row is-folder" onClick={() => toggle(key)} aria-expanded={isOpen(key)}>
        <Icon name="chevronDown" className="aw-tree__chev" />
        <span className="aw-tree__lbl">{label}</span>
        {count != null && <span className="aw-tree__cnt num">{count}</span>}
      </button>
      {isOpen(key) && <ul>{children}</ul>}
    </li>
  );

  return (
    <>
      <div className="aw-pane__head">
        <span className="aw-label aw-pane__title">{view === "tree" ? "資料夾" : view === "search" ? "搜尋" : "這段對話"}</span>
        <button type="button" className={`aw-icon-btn${view === "tree" ? " is-on" : ""}`} onClick={() => setView("tree")} title="資料夾" aria-label="資料夾" aria-pressed={view === "tree"}>
          <Icon name="folder" />
        </button>
        <button
          type="button"
          className={`aw-icon-btn${view === "search" ? " is-on" : ""}`}
          onClick={() => {
            setView("search");
            window.setTimeout(() => input.current?.focus(), 0);
          }}
          title="搜尋知識庫的畫作"
          aria-label="搜尋"
          aria-pressed={view === "search"}
        >
          <Icon name="search" />
        </button>
        <button type="button" className={`aw-icon-btn${view === "mentioned" ? " is-on" : ""}`} onClick={() => setView("mentioned")} title="這段對話談到的作品" aria-label="這段對話談到的作品" aria-pressed={view === "mentioned"}>
          <Icon name="bookmark" />
        </button>
      </div>
      {view === "tree" && (
        <div className="aw-files-tools">
          <button type="button" className={`aw-icon-btn${byYear ? " is-on" : ""}`} onClick={() => setByYear((x) => !x)} title={byYear ? "改依名稱排序" : "改依年代排序"} aria-label="排序" aria-pressed={byYear}>
            <Icon name="sort" />
          </button>
          <button type="button" className="aw-icon-btn" onClick={collapseAll} title="全部收合" aria-label="全部收合">
            <Icon name="collapse" />
          </button>
        </div>
      )}
      {view === "search" && (
        <div className="aw-files-search">
          <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder="畫名、畫家、風格標籤" aria-label="搜尋知識庫的畫作" />
        </div>
      )}
      <nav className="aw-tree" aria-label="知識庫的畫作">
        {isLoading && <p className="aw-tree__empty">讀取知識庫…</p>}
        {error && <p className="aw-tree__empty">讀不到知識庫的畫作清單：{(error as Error).message}</p>}
        {data && view === "tree" && (
          <ul>
            {folder(
              "kb",
              "知識庫",
              null,
              folder(
                "art",
                "畫作",
                items.length,
                groups.map((g) => folder(`artist:${g.artist}`, g.artist, g.works.length, g.works.map((a) => file(a, 1)))),
              ),
            )}
          </ul>
        )}
        {data && view === "search" && (query ? found.length ? <ul>{found.map((a) => file(a))}</ul> : <p className="aw-tree__empty">沒有符合「{q.trim()}」的畫作。</p> : <p className="aw-tree__empty">輸入畫名、畫家或風格標籤，點結果在中間開成分頁。</p>)}
        {view === "mentioned" &&
          (mentioned.length ? (
            <ul>{mentioned.map((m) => (items.find((a) => a.id === m.id) ? file(items.find((a) => a.id === m.id)!) : <li key={m.id}><button type="button" className={`aw-tree__row${m.id === currentId ? " is-on" : ""}`} style={{ paddingLeft: 8 }} onClick={() => onOpen(m.id)}><Icon name="image" /><span className="aw-tree__lbl">{m.label}</span></button></li>))}</ul>
          ) : (
            <p className="aw-tree__empty">這段對話還沒有談到任何作品。</p>
          ))}
      </nav>
      <div className="aw-vault">
        <Mark />
        <span className="aw-vault__name">ARTRAG 知識庫</span>
        <span className="aw-vault__info" title={data ? `畫作 ${items.length} 件・知識庫版本 ${data.kb_version}` : "讀取中"}>
          <Icon name="info" />
        </span>
      </div>
    </>
  );
}
