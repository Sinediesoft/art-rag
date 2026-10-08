import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, assetUrl, type ArtworkSummary, type PlannedWorkOrder } from "../../api/client";
import { useArtwork, useArtworkColors, useArtworks, usePart, useProductionOverview, useTextSearch } from "../../api/hooks";
import type { CadDoneEvent, CadResultEvent } from "../../api/sse";
import { formatTaipei, seconds } from "../../lib/format";
import type { Output } from "../../shell/outputs";
import { ModelViewer } from "../LazyModelViewer";
import { GanttChart, GanttLegend, type GanttOrder } from "../schedule/GanttChart";
import { Icon, Spinner } from "./Icons";
import { ZoomPan } from "./ZoomPan";

/**
 * 來源：ArtRAG-前端demo/source/src/components/Viewers.tsx 的版面（大圖、展品標籤、標題欄、資料表、甘特圖、年表、相似作品）。
 * 內容全部來自真實 API：每個檢視自己用 React Query 讀資料（同一份快取也給功能頁用），
 * 讀的時候帶目前的 JWT，看不到的資料由後端回 403，這裡只顯示錯誤，不顯示內容。
 */
const short = (s: string) => s.split("（")[0];

/**
 * 量容器大小：用 callback ref，元素什麼時候掛上（例如先顯示讀取中、資料回來才出現）就什麼時候開始量，
 * 換掉或卸載時停止觀察
 */
export function useSize<T extends HTMLElement>() {
  const [size, setSize] = useState({ w: 0, h: 0 });
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: T | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    const measure = () => setSize((s) => (s.w === el.clientWidth && s.h === el.clientHeight ? s : { w: el.clientWidth, h: el.clientHeight }));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    observer.current = new ResizeObserver(measure);
    observer.current.observe(el);
  }, []);
  return [ref, size] as const;
}

function Loading({ label = "讀取中…" }: { label?: string }) {
  return (
    <div className="view view--empty">
      <p className="pending">
        <Spinner />
        {label}
      </p>
    </div>
  );
}

function Failed({ error }: { error: unknown }) {
  const e = error as ApiError;
  return (
    <div className="view view--empty" role="alert">
      <p className="view__error">
        <Icon name="lock" />
        {e?.status === 403 ? "目前身分看不到這份資料" : e?.status === 404 ? "找不到這份資料" : "讀取失敗"}
        <small>
          {e?.message}
          {e?.code ? `（${e.code}）` : ""}
        </small>
      </p>
    </div>
  );
}

// ================================================================ 工廠：圖紙

export function DrawingView({ o }: { o: Output }) {
  const { data: p, error, isLoading } = usePart(o.partId);
  if (isLoading) return <Loading label="讀取圖紙…" />;
  if (error || !p) return <Failed error={error} />;
  return (
    <div className="view view--drawing">
      <div className="view__canvas">
        <ZoomPan src={assetUrl(p.drawing_url)} alt={p.name.zh} paper />
      </div>
      <dl className="titleblock">
        {[
          ["品名", p.name.zh],
          ["料號", p.part_no],
          ["圖號", `${p.drawing_no} rev.${p.revision}`],
          ["材料", p.material],
          ["表面處理", p.surface ?? "—"],
          ["機密等級", p.confidentiality],
        ].map(([k, v]) => (
          <div key={k} className={`titleblock__cell${k === "機密等級" && v === "機密" ? " is-secret" : ""}`}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ================================================================ 工廠：庫存與查詢（Text-to-SQL 的唯讀結果）

export function DataTable({ columns, rows }: { columns: string[]; rows: (string | number | null)[][] }) {
  const numeric = columns.map((_, j) => rows.some((r) => typeof r[j] === "number"));
  return (
    <div className="table-wrap view__table">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c, j) => (
              <th key={c + j} className={numeric[j] ? "num" : ""}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={typeof v === "number" ? "num" : ""}>
                  {v ?? "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 單一數列的橫條：同一個色相、數值標在條末 */
function Bars({ items, unit }: { items: { label: string; sub?: string; value: number }[]; unit: string }) {
  const max = Math.max(1, ...items.map((i) => i.value)) * 1.12;
  return (
    <ul className="bars" role="list">
      {items.map((it, i) => (
        <li key={i} className="bars__row" title={`${it.label}${it.sub ? `・${it.sub}` : ""}：${it.value} ${unit}`}>
          <span className="bars__label">
            {it.label}
            {it.sub && <small>{it.sub}</small>}
          </span>
          <span className="bars__track">
            <span className="bars__fill" style={{ width: `${(Math.max(0, it.value) / max) * 100}%` }} />
            <span className="bars__value num" style={{ left: `${(Math.max(0, it.value) / max) * 100}%` }}>
              {it.value}
            </span>
          </span>
          <span className="bars__flag" />
        </li>
      ))}
    </ul>
  );
}

const cleanSql = (text: string) => text.replace(/```(?:sql|sqlite)?\n?/gi, "").trim();

/** 從紀錄還原的查詢：結果是工廠內部資料，沒有存進瀏覽器 */
function StaleQuery({ o, onRerun }: { o: Output; onRerun?: (turnId: string) => void }) {
  return (
    <div className="view view--empty">
      <p className="view__error">
        <Icon name="lock" />
        這份查詢結果沒有存進瀏覽器
        <small>庫存、訂單、工單是工廠內部資料，重新整理或換身分後要以目前身分重新查詢（後端會重新判斷權限）。</small>
      </p>
      {onRerun && (
        <button type="button" className="btn btn--primary" onClick={() => onRerun(o.turnId)}>
          以目前身分重新查詢
        </button>
      )}
    </div>
  );
}

export function StockView({ o, onRerun }: { o: Output; onRerun?: (turnId: string) => void }) {
  if (o.stale || !o.sql?.result) return <StaleQuery o={o} onRerun={onRerun} />;
  const p = o.sql;
  const res = p.result!;
  const cols = res.columns;
  // 有數量欄就畫橫條：第一個數值欄當數值、第一個文字欄當標籤（欄名由 SQL 決定，不猜意義）
  const numIdx = cols.findIndex((_, j) => res.rows.length > 0 && res.rows.every((r) => typeof r[j] === "number" || r[j] === null));
  const labelIdx = cols.findIndex((_, j) => res.rows.some((r) => typeof r[j] === "string"));
  const chart =
    numIdx >= 0 && labelIdx >= 0 && res.rows.length > 1 && res.rows.length <= 24
      ? res.rows.map((r) => ({ label: String(r[labelIdx] ?? "—"), value: Number(r[numIdx] ?? 0) }))
      : null;
  const sql = p.attempts.filter((a) => a.ok).at(-1)?.sql ?? p.attempts.at(-1)?.sql ?? "";
  return (
    <div className="view view--data">
      <div className="view__scroll">
        {res.rows.length === 1 && numIdx >= 0 && (
          <div className="hero-figure">
            <span className="hero-figure__value num">{res.rows[0][numIdx]}</span>
            <span className="hero-figure__label">{cols[numIdx]}</span>
          </div>
        )}
        {p.answer && (
          <section className="view__section">
            <p className="view__label">回答（本地 LLM 依查詢結果）</p>
            <p className="view__answer">{p.answer.replace(/\s?\[\d+\]/g, "")}</p>
          </section>
        )}
        {chart && (
          <section className="view__section">
            <p className="view__label">
              {cols[numIdx]}・依{cols[labelIdx]}
            </p>
            <Bars unit="" items={chart} />
          </section>
        )}
        <section className="view__section">
          <p className="view__label">
            明細・{res.row_count} 筆{res.truncated ? "（已截斷）" : ""}
          </p>
          {res.rows.length ? <DataTable columns={cols} rows={res.rows} /> : <p className="empty">查無資料。</p>}
          {sql && (
            <details className="view__sql">
              <summary>
                查詢語法（唯讀・{res.exec_ms} ms・{p.done?.model ?? ""}）
              </summary>
              <pre className="code code--block">{cleanSql(sql)}</pre>
            </details>
          )}
        </section>
      </div>
    </div>
  );
}

// ================================================================ 工廠：3D（Ortho2CAD 的 STL）

export function ModelView({ o }: { o: Output }) {
  const [ref, size] = useSize<HTMLDivElement>();
  // 從紀錄還原：以工作編號重新讀取（後端依目前的 JWT 檢查看不看得到那張圖紙）
  const saved = useQuery({ queryKey: ["cad-job", o.cadJobId], queryFn: () => api.cadJob(o.cadJobId!), enabled: !o.job && !!o.cadJobId, retry: false });
  const res = (o.job?.result ?? (saved.data?.result as unknown as CadResultEvent | undefined)) || null;
  const done = o.job?.done ?? (saved.data?.done as unknown as CadDoneEvent | undefined) ?? null;
  if (!res) return saved.error ? <Failed error={saved.error} /> : <Loading label="讀取 3D 模型…" />;
  return (
    <div className="view view--model">
      <div ref={ref} className="view__canvas view__canvas--model">
        {size.h > 0 && <ModelViewer layers={[{ url: res.files["model.stl"], color: "#9a9a9a" }]} height={Math.max(240, size.h)} />}
      </div>
      <div className="view__foot">
        <dl className="specs">
          {(
            [
              ["外框尺寸 mm", res.dims ? `${Math.round(res.dims.width)}×${Math.round(res.dims.depth)}×${Math.round(res.dims.height)}` : "—"],
              ["面數", res.faces ?? "—"],
              ["與標準模型 IoU", res.iou != null ? res.iou.toFixed(2) : "未收錄"],
              ["總耗時", done ? seconds(done.latency_ms.total) : "—"],
            ] as [string, ReactNode][]
          ).map(([k, v]) => (
            <div key={k} className="specs__item">
              <dd className="specs__value">{v}</dd>
              <dt className="specs__label">{k}</dt>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

// ================================================================ 工廠：排程（甘特圖）

export function ScheduleView({ o }: { o: Output }) {
  const { data, error, isLoading } = useProductionOverview();
  const saved = useQuery({ queryKey: ["schedule-run", o.runId], queryFn: () => api.scheduleRun(o.runId!), enabled: !!o.runId, retry: false });
  const [view, setView] = useState<"machine" | "order">("machine");
  if (isLoading || (o.runId && saved.isLoading)) return <Loading label="讀取生產排程…" />;
  if (error || !data) return <Failed error={error} />;
  if (saved.error) return <Failed error={saved.error} />;
  const run = o.solve?.solution ?? saved.data ?? data.current;
  if (!run || !run.operations.length)
    return (
      <div className="view view--empty">
        <p className="view__error">
          還沒有排程結果
          <small>在左邊說「重新排程」並確認，或到生產排程頁開始排程。</small>
        </p>
      </div>
    );
  const planned: PlannedWorkOrder[] = run.work_orders;
  const orders: GanttOrder[] = planned.map((w) => ({ wo_no: w.wo_no, part_name: w.part_name, priority: w.priority, qty: w.qty, due_min: w.due_min, late_min: w.late_min }));
  const k = run.kpis;
  const created = "created_at" in run ? formatTaipei(run.created_at) : null;
  return (
    <div className="view view--data">
      <div className="view__scroll">
        <section className="view__section">
          <dl className="specs">
            {(
              [
                ["工單／工序", `${k.n_work_orders}／${run.operations.length}`],
                ["準時交貨", `${k.n_work_orders - k.n_late}／${k.n_work_orders}`],
                ["會延遲", `${k.n_late} 張`],
                ["全部完工", k.finish_at ? k.finish_at.slice(5, 16).replace("T", " ") : "—"],
              ] as [string, ReactNode][]
            ).map(([kk, v]) => (
              <div key={kk} className="specs__item">
                <dd className="specs__value">{v}</dd>
                <dt className="specs__label">{kk}</dt>
              </div>
            ))}
          </dl>
        </section>
        <section className="view__section">
          <div className="view__row">
            <p className="view__label">
              {o.solve
                ? `這次的排程結果・${o.solve.meta?.engine_label ?? ""}`
                : saved.data
                  ? `這次的排程結果・${saved.data.engine_label}${created ? `・${created}` : ""}`
                  : `目前排程${created ? `・${created}` : ""}`}
              ・{data.machines.length} 台機台
            </p>
            <div className="segmented segmented--small" role="radiogroup" aria-label="甘特圖檢視">
              {(
                [
                  ["machine", "機台"],
                  ["order", "工單"],
                ] as const
              ).map(([kk, label]) => (
                <button key={kk} type="button" role="radio" aria-checked={view === kk} className={view === kk ? "is-on" : ""} onClick={() => setView(kk)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="legacy gantt-wrap">
            <GanttChart machines={data.machines} operations={run.operations} orders={orders} axis={run.axis} dayMinutes={data.calendar.day_minutes} view={view} />
            <GanttLegend view={view} />
          </div>
        </section>
        <section className="view__section">
          <p className="view__label">工單明細</p>
          <DataTable
            columns={["工單", "零件", "數量", "優先", "延遲（分）"]}
            rows={planned.map((w) => [w.wo_no, w.part_name, w.qty, w.priority, w.late_min ?? 0])}
          />
        </section>
      </div>
    </div>
  );
}

// ================================================================ 藝術：作品

function MuseumLabel({ id }: { id: string }) {
  const { data: a } = useArtwork(id);
  if (!a) return null;
  return (
    <div className="museum-label">
      <p className="museum-label__title">{a.title.zh}</p>
      {a.title.en && <p className="museum-label__en">{a.title.en}</p>}
      <p className="museum-label__by">
        {a.artist.zh}・{short(a.date_text)}
      </p>
      {a.medium && <p className="museum-label__fine">{a.medium}</p>}
      <p className="museum-label__fine">{short(a.collection)}</p>
      {a.style_tags.length > 0 && (
        <p className="museum-label__tags">
          {a.style_tags.map((t) => (
            <span key={t}>{t}</span>
          ))}
        </p>
      )}
    </div>
  );
}

export function ArtworkView({ o }: { o: Output }) {
  const { data: a, error, isLoading } = useArtwork(o.artworkId);
  if (isLoading) return <Loading label="讀取作品…" />;
  if (error || !a) return <Failed error={error} />;
  return (
    <div className="view view--artwork">
      <div className="view__canvas view__canvas--wall">
        <ZoomPan src={assetUrl(a.image_url)} alt={a.title.zh} />
      </div>
      <div className="view__foot view__foot--label">
        <MuseumLabel id={a.id} />
        <p className="view__credit">
          圖片來源：{a.image.attribution ?? short(a.collection)}・{a.image.license}
        </p>
      </div>
    </div>
  );
}

// ================================================================ 藝術：細看（知識庫段落＋色彩分析）

const FOCUS_TOPIC = /技法|筆|色|構圖|畫面|風格/;

/**
 * 細看：大圖可以縮放平移，疊上色塊分布圖（/artworks/{id}/colors 的 map_url）看主色在哪裡；
 * 右下是知識庫段落（依主題）與主色。API 沒有畫面上的座標標註，所以不畫編號熱點（見 Implementation Report 的契約缺口）。
 */
export function DetailView({ o }: { o: Output }) {
  const { data: a, error, isLoading } = useArtwork(o.artworkId);
  const { data: colors } = useArtworkColors(o.artworkId);
  const [overlay, setOverlay] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  if (isLoading) return <Loading label="讀取作品…" />;
  if (error || !a) return <Failed error={error} />;
  const notes = a.descriptions.filter((d) => d.lang === "zh" || a.descriptions.every((x) => x.lang !== "zh"));
  const match = (topic?: string | null) => (o.focus === "技法" ? FOCUS_TOPIC.test(topic ?? "") : !!topic);
  return (
    <div className="view view--detail">
      <div className="view__canvas view__canvas--wall">
        <ZoomPan
          src={assetUrl(overlay && colors ? colors.map_url : a.image_url)}
          alt={overlay ? `〈${a.title.zh}〉的色塊分布` : a.title.zh}
          label={
            colors ? (
              <button type="button" className="zp__overlay" onClick={() => setOverlay((x) => !x)} aria-pressed={overlay}>
                <Icon name="layers" strokeWidth={1.8} />
                {overlay ? "看原圖" : "疊色塊分布"}
              </button>
            ) : null
          }
        />
      </div>
      <ol className="notes">
        {colors && (
          <li>
            <div className="note note--palette">
              <span className="note__n">
                <Icon name="palette" />
              </span>
              <span className="note__body">
                <span className="note__title">
                  主色<small>色彩分析</small>
                </span>
                <span className="palette">
                  {colors.palette.slice(0, 6).map((c) => (
                    <span key={c.hex} className="palette__chip" title={`${c.name}・${Math.round(c.share * 100)}%`}>
                      <span style={{ background: c.hex }} />
                      {c.name} {Math.round(c.share * 100)}%
                    </span>
                  ))}
                </span>
                <span className="note__text">{colors.summary}</span>
              </span>
            </div>
          </li>
        )}
        {notes.map((d, i) => (
          <li key={i}>
            <button type="button" className={`note${sel === i ? " is-on" : ""}${match(d.topic) ? " is-match" : ""}`} onClick={() => setSel(sel === i ? null : i)} aria-expanded={sel === i}>
              <span className="note__n">{i + 1}</span>
              <span className="note__body">
                <span className="note__title">
                  {d.topic ?? "說明"}
                  <small>{d.source_url ? d.license : (d.source ?? d.license)}</small>
                </span>
                <span className={`note__text${sel === i ? "" : " is-clamp"}`}>{d.text}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

// ================================================================ 藝術：年表（館藏作品依年代）

const yearOf = (s: string) => {
  const m = s.match(/(\d{3,4})/);
  return m ? Number(m[1]) : null;
};

/**
 * 年表：知識庫裡的作品依年代排列，本作與同一位畫家的作品標出來；同時期（±40 年）的其他館藏也列入，點了接著問。
 * 畫家生平事件 API 沒有提供，這裡只列有出處的作品與「創作背景」類段落。
 */
export function TimelineView({ o, onAsk }: { o: Output; onAsk: (q: string) => void }) {
  const { data: a, error, isLoading } = useArtwork(o.artworkId);
  const { data: list } = useArtworks();
  const events = useMemo(() => {
    if (!a || !list) return [];
    const y0 = yearOf(a.date_text);
    return list.items
      .map((x) => ({ x, year: yearOf(x.date_text) }))
      .filter(({ x, year }) => x.id === a.id || x.artist_zh === a.artist.zh || (y0 != null && year != null && Math.abs(year - y0) <= 40))
      .sort((p, q) => (p.year ?? 9999) - (q.year ?? 9999));
  }, [a, list]);
  if (isLoading) return <Loading label="讀取作品…" />;
  if (error || !a) return <Failed error={error} />;
  const bg = a.descriptions.filter((d) => /背景|生平|歷史|創作|時代|版本/.test(d.topic ?? ""));
  const ys = events.map((e) => e.year).filter((y): y is number => y != null);
  return (
    <div className="view view--timeline">
      <div className="view__scroll">
        <header className="tl-head">
          <img src={assetUrl(a.thumb_url)} alt="" />
          <div>
            <p className="view__label">{a.artist.zh}</p>
            <p className="tl-head__span">{ys.length ? `${Math.min(...ys)}–${Math.max(...ys)}` : short(a.date_text)}</p>
            <p className="tl-head__work">
              〈{a.title.zh}〉・{short(a.date_text)}
            </p>
          </div>
        </header>
        <ol className="tl">
          {events.map(({ x, year }) => {
            const focus = x.id === a.id;
            const same = x.artist_zh === a.artist.zh;
            return (
              <li key={x.id} className={`tl__item${focus ? " is-focus" : ""}`}>
                <span className="tl__year num">{year ?? "—"}</span>
                <span className="tl__dot" aria-hidden />
                <div className="tl__body">
                  <p className="tl__title">
                    〈{x.title_zh}〉
                    {focus && <span className="tl__tag">本作</span>}
                  </p>
                  <p className="tl__note">
                    {x.artist_zh}・{short(x.date_text)}・{short(x.collection)}
                    {!same && "・同時期館藏"}
                  </p>
                  {!focus && (
                    <button type="button" className="tl__link" onClick={() => onAsk(`介紹一下〈${x.title_zh}〉`)}>
                      <img src={assetUrl(x.thumb_url)} alt="" />
                      問這幅〈{x.title_zh}〉
                      <Icon name="chevronRight" strokeWidth={2} />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
        {bg.length > 0 && (
          <section className="view__section">
            <p className="view__label">創作背景（知識庫段落）</p>
            {bg.map((d, i) => (
              <p key={i} className="view__answer">
                <b>{d.topic}</b>　{d.text}
              </p>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}

// ================================================================ 藝術：相似作品

function Score({ value }: { value: number }) {
  const v = Math.max(0, Math.min(1, value));
  return (
    <span className="score" title={`分數 ${value.toFixed(2)}`}>
      <span className="score__track">
        <span className="score__fill" style={{ width: `${v * 100}%` }} />
      </span>
      <span className="score__value num">{value.toFixed(2)}</span>
    </span>
  );
}

/**
 * 相似作品：以文搜畫的結果（使用者的搜尋句），或以作品的風格標籤＋畫家搜尋（「有沒有風格相近的」，排除本作）。
 * 並排比較用 /compare/items（直接讀知識庫欄位，不經生成）。
 */
export function SimilarView({ o, onAsk }: { o: Output; onAsk: (q: string) => void }) {
  const { data: base } = useArtwork(o.search ? undefined : o.artworkId);
  const q = base ? [...base.style_tags.slice(0, 3), base.medium ?? ""].filter(Boolean).join(" ") || base.title.zh : null;
  const search = useTextSearch(o.items ? null : q);
  const items: { artwork: ArtworkSummary; score: number }[] = o.items ?? (search.data?.results ?? []).filter((r) => r.artwork.id !== base?.id).map((r) => ({ artwork: r.artwork, score: r.score }));
  const [cmp, setCmp] = useState<string | null>(null);
  const compare = useQuery({
    queryKey: ["compare", `artwork:${base?.id}`, `artwork:${cmp}`],
    queryFn: () => api.compareItems(`artwork:${base!.id}`, `artwork:${cmp}`),
    enabled: !!base && !!cmp,
  });

  if (!o.items && (search.isLoading || !base)) return <Loading label="搜尋相近作品…" />;
  if (search.error) return <Failed error={search.error} />;

  if (o.search || !base)
    return (
      <div className="view view--similar">
        <div className="view__scroll">
          <p className="view__label">「{o.search}」・依 Chinese-CLIP＋bge-m3 的分數排序</p>
          <div className="wall">
            {items.map(({ artwork: a, score }, i) => (
              <button key={a.id} type="button" className={`wall__item${i === 0 ? " is-best" : ""}`} onClick={() => onAsk(`介紹一下〈${a.title_zh}〉`)}>
                <span className="wall__img">
                  <img src={assetUrl(a.thumb_url)} alt="" />
                </span>
                <span className="wall__flag">{i === 0 ? "最符合" : `第 ${i + 1} 名`}</span>
                <span className="wall__title">{a.title_zh}</span>
                <span className="wall__meta">
                  {a.artist_zh}・{short(a.date_text)}
                </span>
                <Score value={score} />
              </button>
            ))}
          </div>
        </div>
      </div>
    );

  const other = cmp ? items.find((x) => x.artwork.id === cmp)?.artwork : null;
  return (
    <div className="view view--similar">
      <div className="view__scroll">
        {!other ? (
          <>
            <div className="sim-base">
              <img src={assetUrl(base.thumb_url)} alt="" />
              <div>
                <p className="view__label">比對基準</p>
                <p className="sim-base__title">{base.title.zh}</p>
                <p className="sim-base__meta">
                  {base.artist.zh}・{short(base.date_text)}・以「{q}」搜尋
                </p>
              </div>
            </div>
            <p className="view__label">依分數排序・點「並排比較」看兩件的欄位差異</p>
            {items.length === 0 && <p className="empty">知識庫裡沒有其他相近的作品。</p>}
            <ul className="sim-list">
              {items.map(({ artwork: a, score }) => (
                <li key={a.id} className="sim">
                  <img src={assetUrl(a.thumb_url)} alt="" />
                  <div className="sim__body">
                    <p className="sim__title">{a.title_zh}</p>
                    <p className="sim__meta">
                      {a.artist_zh}・{short(a.date_text)}
                    </p>
                    <Score value={score} />
                    <div className="sim__actions">
                      <button type="button" className="btn btn--primary" onClick={() => setCmp(a.id)}>
                        並排比較
                      </button>
                      <button type="button" className="btn btn--ghost" onClick={() => onAsk(`介紹一下〈${a.title_zh}〉`)}>
                        介紹這幅
                      </button>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <div className="compare">
            <button type="button" className="compare__back" onClick={() => setCmp(null)}>
              <Icon name="chevronLeft" strokeWidth={2} />
              回到相似作品
            </button>
            <div className="compare__pair">
              {[
                { id: base.id, title: base.title.zh, artist: base.artist.zh, src: base.thumb_url },
                { id: other.id, title: other.title_zh, artist: other.artist_zh, src: other.thumb_url },
              ].map((x) => (
                <figure key={x.id}>
                  <span className="compare__img">
                    <img src={assetUrl(x.src)} alt={x.title} />
                  </span>
                  <figcaption>
                    {x.title}
                    <small>{x.artist}</small>
                  </figcaption>
                </figure>
              ))}
            </div>
            {compare.isLoading && <p className="pending"><Spinner />讀取兩件的資料…</p>}
            {compare.error && <p className="view__error">{(compare.error as Error).message}</p>}
            {compare.data && (
              <>
                <p className="view__label">
                  {compare.data.differences} 個欄位不同・直接讀知識庫，不經生成
                </p>
                <table className="table compare__table">
                  <tbody>
                    {compare.data.rows
                      .filter((r) => r.a || r.b)
                      .map((r) => (
                        <tr key={r.key} className={r.same ? "is-same" : ""}>
                          <th>{r.label}</th>
                          <td>{r.a ?? "—"}</td>
                          <td>{r.b ?? "—"}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
