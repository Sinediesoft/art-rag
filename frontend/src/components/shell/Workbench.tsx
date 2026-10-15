import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { Output, OutputKind, Subject } from "../../shell/outputs";
import { dispatchOf } from "../../shell/runner";
import type { Conv } from "../../shell/types";
import { Icon } from "./Icons";
import { downloadOf } from "./Showcase";
import { DrawingView, ModelView, ScheduleView, StockView } from "./Viewers";

/**
 * 來源：demo/factory-workbench/index.html（組內同意的改版方向）。
 * 工廠模組的右半邊：四個功能板塊（圖紙、3D、排程、庫存）同時擺在工作台上，對話產生的成果放進對應的板塊。
 * 板塊之間的縫可以拖曳調整大小；右上角可以聚焦、最大化、收合；上方有「四格／設計檢視／生產檢視」三種版面。
 * 板塊的內容全部來自真實 API（Viewers.tsx 各自帶目前的 JWT 讀取）；寫入（重新排程、庫存異動）一律在左邊對話裡
 * 走後端的試算 → 確認／主管核准，板塊只顯示進度與結果，不在前端改任何數字。
 */
export type PanelId = "drawing" | "model" | "schedule" | "stock";

export const PANELS: { id: PanelId; n: string; name: string; en: string; icon: string }[] = [
  { id: "drawing", n: "01", name: "圖紙", en: "DRAWING", icon: "frame" },
  { id: "model", n: "02", name: "3D", en: "MODEL", icon: "cube" },
  { id: "schedule", n: "03", name: "排程", en: "SCHEDULE", icon: "calendar" },
  { id: "stock", n: "04", name: "庫存", en: "INVENTORY", icon: "box" },
];
const NAME = Object.fromEntries(PANELS.map((p) => [p.id, p.name])) as Record<PanelId, string>;

const KINDS: Record<PanelId, OutputKind[]> = { drawing: ["drawing"], model: ["model"], schedule: ["schedule"], stock: ["stock", "query"] };

/** 成果放進哪個板塊（庫存板塊也收一般的唯讀查詢結果：訂單、工單） */
export function panelOf(kind: OutputKind): PanelId | null {
  return PANELS.find((p) => KINDS[p.id].includes(kind))?.id ?? null;
}

// ================================================================ 版面

export interface Layout {
  /** 收合到下方的板塊 */
  min: PanelId[];
  /** 聚焦：放大在左邊，其他排在右邊一欄 */
  focus: PanelId | null;
  /** 最大化：只顯示這一塊 */
  max: PanelId | null;
  /** 四格的直縫、橫縫、聚焦時的直縫（比例） */
  col: number;
  row: number;
  fcol: number;
}

export const DEFAULT_LAYOUT: Layout = { min: [], focus: null, max: null, col: 0.5, row: 0.54, fcol: 0.64 };

/** 板塊的位置與大小：[x, y, w, h]，都是工作台的比例 */
export type Rect = [number, number, number, number];
export interface Gutter {
  axis: "x" | "y";
  key: "col" | "row" | "fcol";
  at: number;
  /** 縫的範圍（另一個方向的比例） */
  a: number;
  b: number;
}

/** 依版面算出每個看得到的板塊在哪裡、哪幾條縫可以拖曳 */
export function arrange(L: Layout): { rects: Partial<Record<PanelId, Rect>>; gutters: Gutter[] } {
  const vis = PANELS.map((p) => p.id).filter((id) => !L.min.includes(id));
  const rects: Partial<Record<PanelId, Rect>> = {};
  const gutters: Gutter[] = [];
  if (L.max && vis.includes(L.max)) {
    rects[L.max] = [0, 0, 1, 1];
    return { rects, gutters };
  }
  const n = vis.length;
  if (!n) return { rects, gutters };
  if (L.focus && vis.includes(L.focus) && n > 1) {
    const others = vis.filter((id) => id !== L.focus);
    const c = L.fcol;
    rects[L.focus] = [0, 0, c, 1];
    others.forEach((id, i) => (rects[id] = [c, i / others.length, 1 - c, 1 / others.length]));
    gutters.push({ axis: "x", key: "fcol", at: c, a: 0, b: 1 });
    return { rects, gutters };
  }
  const { col: c, row: r } = L;
  if (n === 1) rects[vis[0]] = [0, 0, 1, 1];
  else if (n === 2) {
    rects[vis[0]] = [0, 0, c, 1];
    rects[vis[1]] = [c, 0, 1 - c, 1];
    gutters.push({ axis: "x", key: "col", at: c, a: 0, b: 1 });
  } else if (n === 3) {
    rects[vis[0]] = [0, 0, c, r];
    rects[vis[1]] = [c, 0, 1 - c, r];
    rects[vis[2]] = [0, r, 1, 1 - r];
    gutters.push({ axis: "x", key: "col", at: c, a: 0, b: r }, { axis: "y", key: "row", at: r, a: 0, b: 1 });
  } else {
    rects[vis[0]] = [0, 0, c, r];
    rects[vis[1]] = [c, 0, 1 - c, r];
    rects[vis[2]] = [0, r, c, 1 - r];
    rects[vis[3]] = [c, r, 1 - c, 1 - r];
    gutters.push({ axis: "x", key: "col", at: c, a: 0, b: 1 }, { axis: "y", key: "row", at: r, a: 0, b: 1 });
  }
  return { rects, gutters };
}

export const PRESETS: { id: string; label: string; apply: Pick<Layout, "min" | "focus" | "max"> }[] = [
  { id: "grid", label: "四格", apply: { min: [], focus: null, max: null } },
  { id: "design", label: "設計檢視", apply: { min: [], focus: "drawing", max: null } },
  { id: "prod", label: "生產檢視", apply: { min: [], focus: "schedule", max: null } },
];

/** 目前的版面對應哪個預設（自己調過收合、最大化就都不是） */
export function presetOf(L: Layout) {
  if (L.max || L.min.length) return null;
  return !L.focus ? "grid" : L.focus === "drawing" ? "design" : L.focus === "schedule" ? "prod" : null;
}

const clamp = (v: number) => Math.min(0.78, Math.max(0.22, v));
const GAP = 10;
const pct = (v: number) => `${+(v * 100).toFixed(3)}%`;

/** 比例換成 CSS：板塊之間留 10px 的縫，貼邊的那一側不留 */
function placement([x, y, w, h]: Rect): CSSProperties {
  const g = GAP / 2;
  const l = x > 0.001 ? g : 0;
  const t = y > 0.001 ? g : 0;
  const r = x + w < 0.999 ? g : 0;
  const b = y + h < 0.999 ? g : 0;
  return {
    left: `calc(${pct(x)} + ${l}px)`,
    top: `calc(${pct(y)} + ${t}px)`,
    width: `calc(${pct(w)} - ${l + r}px)`,
    height: `calc(${pct(h)} - ${t + b}px)`,
  };
}

/** 版面只是這個瀏覽器的偏好：讀寫失敗（私密視窗、封鎖儲存）就用預設 */
const LAYOUT_KEY = "artrag-workbench";
const isPanel = (v: unknown): v is PanelId => PANELS.some((p) => p.id === v);
function loadLayout(): Layout {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "null") as Partial<Layout> | null;
    if (!raw || typeof raw !== "object") return DEFAULT_LAYOUT;
    const num = (v: unknown, d: number) => (typeof v === "number" && Number.isFinite(v) ? clamp(v) : d);
    return {
      min: Array.isArray(raw.min) ? [...new Set(raw.min.filter(isPanel))] : [],
      focus: isPanel(raw.focus) ? raw.focus : null,
      max: isPanel(raw.max) ? raw.max : null,
      col: num(raw.col, DEFAULT_LAYOUT.col),
      row: num(raw.row, DEFAULT_LAYOUT.row),
      fcol: num(raw.fcol, DEFAULT_LAYOUT.fcol),
    };
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

// ================================================================ 寫入進度（試算 → 確認／核准都在左邊對話，這裡只顯示狀態）

export interface Pending {
  tone: "wait" | "done" | "warn";
  text: string;
}

/** 庫存、訂單的異動放在庫存板塊；工單的異動放在排程板塊 */
const panelOfOp = (op: string): PanelId | null => (op.startsWith("wo_") ? "schedule" : /stock|^so_/.test(op) ? "stock" : null);

/**
 * 每個板塊最近一次寫入的進度：重新排程（Timefold 求解後寫回）、庫存與工單異動（/changes 試算 → 確認或送主管核准）。
 * 只用後端回來的狀態與編號，不顯示試算的數字（數字在左邊的異動卡上，看得到才顯示）。
 */
export function pendingWrites(conv: Conv): Partial<Record<PanelId, Pending>> {
  const out: Partial<Record<PanelId, Pending>> = {};
  for (const t of conv.turns) {
    const p = t.part;
    if (p?.kind === "schedule") {
      if (p.cancelled || p.job?.status === "cancelled") delete out.schedule;
      else if (!p.job) out.schedule = { tone: "wait", text: "重新排程還沒開始：在左邊對話按「開始排程」才會求解，結果寫回後放進這個板塊。" };
      else if (["queued", "starting", "solving"].includes(p.job.status))
        out.schedule = { tone: "wait", text: p.job.stopping ? "正在提前結束求解，採用目前最佳解寫回…" : "排程求解中（Timefold），完成後結果放進這個板塊。" };
      else if (p.job.status === "error") out.schedule = { tone: "warn", text: "這次重新排程沒有完成，現行排程沒有變動。" };
      else delete out.schedule;
    }
    if (p?.kind === "change") {
      const op = p.preview?.op ?? p.committed?.op ?? p.approval?.op ?? (t.route ? dispatchOf(t.route).op : null) ?? "";
      const id = panelOfOp(op);
      if (!id) continue;
      const label = p.preview?.op_label ?? p.approval?.op_label ?? "異動";
      const what = id === "stock" ? "庫存" : "排程";
      if (p.committed) out[id] = { tone: "done", text: `已寫入 ${p.committed.change_no}：板塊裡的${what}是寫入前查的，再問一次會以最新資料顯示。` };
      else if (p.approval) out[id] = { tone: "wait", text: `已送主管核准（${p.approval.ap_no}）：核准前${what}不會變動。` };
      else if (p.status === "unconfirmed") out[id] = { tone: "warn", text: "寫入已送出、沒有收到回覆：可能已經寫好，先到功能頁確認，不要重送。" };
      else if (p.status === "committing") out[id] = { tone: "wait", text: p.action === "approval" ? "送主管核准中…" : "寫入中…" };
      else if (p.status === "error") out[id] = { tone: "warn", text: `「${label}」沒有完成，${what}沒有變動。` };
      else if (p.status === "previewing") out[id] = { tone: "wait", text: `試算「${label}」中…` };
      else if (p.preview?.next === "confirm") out[id] = { tone: "wait", text: `「${label}」待確認：在左邊對話核對前後數字，按「確認寫入」才會寫入。` };
      else if (p.preview?.next === "approval") out[id] = { tone: "wait", text: `「${label}」超過額度：在左邊對話補一句說明，按「送主管核准」。` };
      else if (p.preview?.next === "need_info") out[id] = { tone: "wait", text: `「${label}」還缺資料：在左邊對話補上。` };
      else delete out[id];
    }
  }
  return out;
}

// ================================================================ 工作台

/** 空板塊的建議問句：送進左邊對話，經 /agent/route 分派，結果回來才放進板塊 */
function asksOf(id: PanelId, subject: Subject | null): string[] {
  const label = subject?.kind === "part" ? subject.label : "連接法蘭";
  switch (id) {
    case "drawing":
      return [`${label}有哪些公差要求？`, "找軸承座的圖紙"];
    case "model":
      return [`把${label}轉成 3D`];
    case "schedule":
      return ["目前的生產排程", "重新排程"];
    case "stock":
      return [`${label}還剩幾件可以出貨？`, "哪些料號低於安全庫存？"];
  }
}

const EMPTY: Record<PanelId, string> = {
  drawing: "問到哪張圖紙，就在這裡大圖顯示，可以縮放、看標題欄。",
  model: "把圖紙轉成 3D（Ortho2CAD，全程在本機），模型會放在這裡旋轉檢視。",
  schedule: "查生產排程或重新排程，甘特圖、交期與延遲工單會放在這裡。",
  stock: "問庫存、訂單、工單，查詢結果（唯讀）會放在這裡。",
};

export function Workbench({
  conv,
  outputs,
  active,
  subject,
  narrow,
  locked,
  onSelect,
  onAsk,
  onRerun,
}: {
  conv: Conv;
  /** 這個模組的成果（時間順序） */
  outputs: Output[];
  /** 目前在看的成果（對話裡點的、或剛完成的） */
  active: Output | undefined;
  subject: Subject | null;
  /** 窄螢幕：板塊改成上下排 */
  narrow: boolean;
  /** 不能送出（回答中、切換身分中）：空板塊的建議問句先停用 */
  locked: boolean;
  onSelect: (key: string) => void;
  onAsk: (q: string) => void;
  onRerun?: (turnId: string) => void;
}) {
  const board = useRef<HTMLDivElement>(null);
  const [L, setL] = useState<Layout>(loadLayout);
  const [dragging, setDragging] = useState(false);
  const setLayout = (patch: Partial<Layout> | ((l: Layout) => Partial<Layout>)) =>
    setL((l) => {
      const next = { ...l, ...(typeof patch === "function" ? patch(l) : patch) };
      saveLayout(next);
      return next;
    });

  // 每個板塊各自記得看到哪一份：目前成果屬於哪一塊就換那一塊，其他板塊不動
  const activePanel = active ? panelOf(active.kind) : null;
  const [pick, setPick] = useState<Partial<Record<PanelId, string>>>(() => (active && activePanel ? { [activePanel]: active.key } : {}));
  const fromPanel = useRef(false);
  const [flash, setFlash] = useState<{ id: PanelId; n: number } | null>(null);
  const seen = useRef(active?.key);
  useEffect(() => {
    if (!active || !activePanel || seen.current === active.key) return;
    seen.current = active.key;
    setPick((p) => (p[activePanel] === active.key ? p : { ...p, [activePanel]: active.key }));
    if (fromPanel.current) {
      fromPanel.current = false;
      return;
    }
    // 對話產生（或在對話裡點了）這個板塊的成果：收合、被別塊最大化時拉回來，閃一下讓眼睛跟過去
    setLayout((l) => ({ min: l.min.filter((x) => x !== activePanel), max: l.max && l.max !== activePanel ? null : l.max }));
    setFlash((f) => ({ id: activePanel, n: (f?.n ?? 0) + 1 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.key]);
  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), 1400);
    return () => window.clearTimeout(t);
  }, [flash]);

  const listOf = (id: PanelId) => outputs.filter((o) => KINDS[id].includes(o.kind));
  const shownOf = (id: PanelId) => {
    const list = listOf(id);
    if (active && activePanel === id) return active;
    return list.find((o) => o.key === pick[id]) ?? list.at(-1);
  };
  const pending = pendingWrites(conv);
  const { rects, gutters } = arrange(L);
  const preset = presetOf(L);
  const visible = PANELS.length - L.min.length;

  const dragGutter = (e: ReactPointerEvent<HTMLDivElement>, g: Gutter) => {
    e.preventDefault();
    const el = e.currentTarget;
    el.setPointerCapture?.(e.pointerId);
    const box = board.current!.getBoundingClientRect();
    setDragging(true);
    document.body.classList.add("is-dragging");
    const move = (ev: PointerEvent) => {
      const size = g.axis === "x" ? box.width : box.height;
      if (size <= 0) return;
      const v = g.axis === "x" ? (ev.clientX - box.left) / size : (ev.clientY - box.top) / size;
      setLayout({ [g.key]: clamp(v) });
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      document.body.classList.remove("is-dragging");
      setDragging(false);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const keyGutter = (e: KeyboardEvent, g: Gutter) => {
    const less = g.axis === "x" ? "ArrowLeft" : "ArrowUp";
    const more = g.axis === "x" ? "ArrowRight" : "ArrowDown";
    if (e.key === less || e.key === more) {
      e.preventDefault();
      setLayout((l) => ({ [g.key]: clamp(l[g.key] + (e.key === more ? 0.02 : -0.02)) }));
    }
    if (e.key === "Home" || e.key === "Enter") setLayout({ [g.key]: DEFAULT_LAYOUT[g.key] });
  };

  return (
    <section className={`workbench${narrow ? " workbench--stack" : ""}`} aria-label="工作台">
      <header className="wb-head">
        <div className="wb-head__title">
          <span className="wb-head__name">工作台</span>
          <span className="wb-head__en">WORKBENCH</span>
          <span className="wb-head__sub">
            {visible} 個板塊
            {L.max ? `・最大化〈${NAME[L.max]}〉` : L.focus && !L.min.includes(L.focus) ? `・聚焦〈${NAME[L.focus]}〉` : ""}・依對話更新
          </span>
        </div>
        <div className="wb-head__tools">
          <div className="segmented segmented--small wb-presets" role="radiogroup" aria-label="版面">
            {PRESETS.map((p) => (
              <button key={p.id} type="button" role="radio" aria-checked={preset === p.id} className={preset === p.id ? "is-on" : ""} onClick={() => setLayout(p.apply)}>
                {p.label}
              </button>
            ))}
          </div>
          <button type="button" className="wb-icon" onClick={() => setLayout(DEFAULT_LAYOUT)} title="重設版面（四格、縫回到中間）" aria-label="重設版面">
            <Icon name="refresh" strokeWidth={1.6} />
          </button>
        </div>
      </header>

      <div ref={board} className={`wb-board${dragging ? " is-dragging" : ""}`}>
        {PANELS.map((meta) => {
          const id = meta.id;
          const rect = rects[id];
          const list = listOf(id);
          const shown = shownOf(id);
          const idx = shown ? list.findIndex((o) => o.key === shown.key) : -1;
          const go = (d: number) => {
            const o = list[(idx + d + list.length) % list.length];
            if (!o) return;
            fromPanel.current = true;
            setPick((p) => ({ ...p, [id]: o.key }));
            onSelect(o.key);
          };
          const dl = shown ? downloadOf(shown) : null;
          const w = pending[id];
          return (
            <section
              key={id}
              data-panel={id}
              className={[
                "wb-panel",
                !rect ? "is-hidden" : "",
                activePanel === id ? "is-active" : "",
                flash?.id === id ? (flash.n % 2 ? "is-flash" : "is-flash2") : "",
              ].join(" ")}
              style={rect && !narrow ? placement(rect) : undefined}
              aria-label={`${meta.name}板塊`}
              aria-hidden={!rect || undefined}
              inert={!rect}
            >
              <header
                className="wb-panel__head"
                onDoubleClick={(e) => {
                  if ((e.target as HTMLElement).closest("button")) return;
                  setLayout((l) => ({ max: l.max === id ? null : id }));
                }}
              >
                <span className="wb-panel__n num">{meta.n}</span>
                <span className="wb-panel__name">{meta.name}</span>
                <span className="wb-panel__en">{meta.en}</span>
                {shown && (
                  <span className="wb-panel__meta" title={`${shown.title}・${shown.meta}`}>
                    {shown.title}
                  </span>
                )}
                {list.length > 1 && (
                  <span className="wb-panel__nav">
                    <button type="button" onClick={() => go(-1)} title="上一份" aria-label={`${meta.name}：上一份`}>
                      <Icon name="chevronLeft" strokeWidth={1.8} />
                    </button>
                    <span className="num">
                      {idx + 1}/{list.length}
                    </span>
                    <button type="button" onClick={() => go(1)} title="下一份" aria-label={`${meta.name}：下一份`}>
                      <Icon name="chevronRight" strokeWidth={1.8} />
                    </button>
                  </span>
                )}
                <div className="wb-panel__acts">
                  {dl && (
                    <button type="button" className="wb-icon" onClick={dl} title="下載" aria-label={`下載${meta.name}`}>
                      <Icon name="download" strokeWidth={1.6} />
                    </button>
                  )}
                  {!narrow && (
                    <button
                      type="button"
                      className={`wb-icon${L.focus === id && !L.max ? " is-on" : ""}`}
                      onClick={() => setLayout((l) => ({ focus: l.focus === id ? null : id, max: null }))}
                      title={L.focus === id ? "取消聚焦" : "聚焦（放大、其他排到旁邊）"}
                      aria-label={`${L.focus === id ? "取消聚焦" : "聚焦"}${meta.name}`}
                      aria-pressed={L.focus === id}
                    >
                      <Icon name={L.focus === id ? "unfocus" : "expand"} strokeWidth={1.6} />
                    </button>
                  )}
                  <button
                    type="button"
                    className={`wb-icon${L.max === id ? " is-on" : ""}`}
                    onClick={() => setLayout((l) => ({ max: l.max === id ? null : id }))}
                    title={L.max === id ? "還原（雙擊標題列也可以）" : "最大化（雙擊標題列也可以）"}
                    aria-label={`${L.max === id ? "還原" : "最大化"}${meta.name}`}
                    aria-pressed={L.max === id}
                  >
                    <Icon name="fit" strokeWidth={1.6} />
                  </button>
                  <button
                    type="button"
                    className="wb-icon"
                    onClick={() =>
                      setLayout((l) => ({ min: [...new Set([...l.min, id])], max: l.max === id ? null : l.max, focus: l.focus === id ? null : l.focus }))
                    }
                    title="收合到下方"
                    aria-label={`收合${meta.name}`}
                  >
                    <Icon name="minus" strokeWidth={1.8} />
                  </button>
                </div>
              </header>
              {w && (
                <p className={`wb-pending wb-pending--${w.tone}`} role="status">
                  {w.text}
                </p>
              )}
              <div className="wb-panel__body">
                {shown ? (
                  <div className="wb-panel__view" key={shown.key}>
                    {id === "drawing" && <DrawingView o={shown} />}
                    {id === "model" && <ModelView o={shown} />}
                    {id === "schedule" && <ScheduleView o={shown} />}
                    {id === "stock" && <StockView o={shown} onRerun={onRerun} />}
                  </div>
                ) : (
                  <div className="wb-empty">
                    <span className="wb-empty__icon">
                      <Icon name={meta.icon} strokeWidth={1.3} />
                    </span>
                    <p className="wb-empty__text">{EMPTY[id]}</p>
                    <div className="wb-empty__asks">
                      {asksOf(id, subject).map((q) => (
                        <button key={q} type="button" className="wb-chip" onClick={() => onAsk(q)} disabled={locked} title="送進左邊的對話">
                          {q}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </section>
          );
        })}
        {!narrow &&
          gutters.map((g) => (
            <div
              key={`${g.axis}:${g.key}`}
              className={`wb-gutter wb-gutter--${g.axis}`}
              style={
                g.axis === "x"
                  ? { left: pct(g.at), top: pct(g.a), height: pct(g.b - g.a) }
                  : { top: pct(g.at), left: pct(g.a), width: pct(g.b - g.a) }
              }
              role="separator"
              aria-orientation={g.axis === "x" ? "vertical" : "horizontal"}
              aria-valuenow={Math.round(L[g.key] * 100)}
              aria-valuemin={22}
              aria-valuemax={78}
              aria-label={g.axis === "x" ? "調整板塊寬度（雙擊還原）" : "調整板塊高度（雙擊還原）"}
              tabIndex={0}
              onPointerDown={(e) => dragGutter(e, g)}
              onKeyDown={(e) => keyGutter(e, g)}
              onDoubleClick={() => setLayout({ [g.key]: DEFAULT_LAYOUT[g.key] })}
            />
          ))}
      </div>

      <footer className="wb-dock">
        <span className="wb-dock__label">已收合</span>
        {L.min.length === 0 ? (
          <span className="wb-dock__empty">沒有。按板塊右上角的「—」可以收到這裡。</span>
        ) : (
          PANELS.filter((p) => L.min.includes(p.id)).map((p) => (
            <button key={p.id} type="button" className="wb-dock__chip" onClick={() => setLayout((l) => ({ min: l.min.filter((x) => x !== p.id) }))} title={`展開${p.name}`}>
              <Icon name={p.icon} strokeWidth={1.5} />
              <b className="num">{p.n}</b>
              {p.name}
              {listOf(p.id).length > 0 && <span className="wb-dock__count num">{listOf(p.id).length}</span>}
            </button>
          ))
        )}
      </footer>
    </section>
  );
}
