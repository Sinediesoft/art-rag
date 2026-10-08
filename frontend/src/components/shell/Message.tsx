import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, assetUrl } from "../../api/client";
import { useStatus, useSwitchAccount } from "../../api/hooks";
import type { SourceItem } from "../../api/sse";
import { seconds, STRATEGY_LABEL } from "../../lib/format";
import { deepActionsFor } from "../../shell/deep";
import { canRerun, REJECTED, writeUnconfirmed } from "../../shell/persist";
import { ctaOf, KIND_LABEL, thumbOf, visiblePart, type Output } from "../../shell/outputs";
import { dispatchOf } from "../../shell/runner";
import { buildStages, egressOf, gatewayStages, progressOf, summaryOf } from "../../shell/stages";
import type { ChangePart, ChatPart, ComparePart, ReconstructPart, SchedulePart, SqlPart, Turn } from "../../shell/types";
import { PhotoStyleGuess } from "../style/StyleGuessCard";
import { Icon, Spinner } from "./Icons";
import { RichText } from "./RichText";
import { TraceDetail, TraceLine } from "./Trace";

/** 問答串共用的動作（入口與模組都用；差別只在 brief／full 與回答下方放什麼） */
export interface ThreadActions {
  mode: "brief" | "full";
  ask: (q: string, forced?: string | null, imageId?: string | null) => void;
  regenerate: () => void;
  rerun: (turnId: string) => void;
  startReconstruct: (turnId: string) => void;
  startSchedule: (turnId: string) => void;
  cancelTask: (turnId: string) => void;
  commitChange: (turnId: string, note: string | null) => void;
  outputsOf: (turnId: string) => Output[];
  activeOutput: string | null;
  showOutput: (key: string) => void;
}

export const ThreadContext = createContext<ThreadActions | null>(null);
const useThread = () => useContext(ThreadContext)!;

const short = (s: string) => s.split("（")[0];

export function UserMessage({ turn, index }: { turn: Turn; index: number }) {
  const text = turn.route?.question || turn.text;
  return (
    <div className="msg msg--user">
      <span className="msg__index">{String(index).padStart(2, "0")}</span>
      {turn.imageId && <img className="msg__photo" src={api.uploadedImageUrl(turn.imageId)} alt="附加的照片" />}
      {(text || !turn.imageId) && <div className="msg__bubble">{text || "（只有照片）"}</div>}
      {turn.forced && <span className="msg__tool">你選的意圖</span>}
      {turn.tamper && <span className="msg__tool">帶竄改過的 JWT</span>}
    </div>
  );
}

/** 一輪的回答：處理軌跡（七段）→ 內容（依分派的模組）→ 成果標籤／跳轉按鈕 → 功能頁連結 → 動作 */
export function AssistantMessage({ turn, isLast, after }: { turn: Turn; isLast: boolean; after?: ReactNode }) {
  const th = useThread();
  const [open, setOpen] = useState(false);
  const r = turn.route;
  const mod = progressOf(turn.part, turn.phase);
  const running = turn.phase === "routing" || turn.phase === "running";
  const stopped = turn.phase === "stopped";
  const blocked = !!r?.blocked || turn.failure?.status === 401;

  const trace = useMemo(() => {
    if (turn.archived)
      return { stages: turn.archived.stages, full: null, summary: turn.archived.summary, egress: null as number | null, calls: [] };
    if (turn.failure?.status === 401 && !r) {
      const st = gatewayStages(turn.failure.code, turn.failure.message);
      return { stages: st, full: st, summary: `閘道拒絕連線（401 ${turn.failure.code}）`, egress: 0, calls: [] };
    }
    if (!r) {
      const st = gatewayStages("", "").map((s) => ({ ...s, state: turn.phase === "routing" ? (s.key === "auth" ? "doing" : "todo") : "skip" }) as typeof s);
      return { stages: st, full: null, summary: turn.failure ? `沒有完成：${turn.failure.message}` : null, egress: null, calls: [] };
    }
    const st = buildStages(r, mod);
    const e = egressOf(r, mod);
    return { stages: st, full: st, summary: summaryOf(r, mod), egress: e.bytes, calls: e.calls };
  }, [turn, r, mod]);

  const finished = !running && !stopped && !turn.failure;
  // 模組裡一律給功能頁連結；入口只在沒有模組跳轉時給（批次辨識、比對不到的照片、系統狀態），交接才有下一步
  const deep = finished && (th.mode === "full" || !ctaOf(turn)) ? deepActionsFor(turn) : [];

  return (
    <div className={`msg msg--ai${blocked ? " is-blocked" : ""}`} data-phase={turn.phase}>
      <div className="msg__body">
        <TraceLine
          stages={trace.stages}
          summary={trace.summary}
          egress={trace.egress}
          running={running && turn.phase === "routing"}
          stopped={stopped && !r}
          blocked={blocked}
          open={open}
          onToggle={trace.full ? () => setOpen((o) => !o) : undefined}
        />
        {open && trace.full && (
          <div className="trace-inline fade-in">
            <TraceDetail stages={trace.full} route={r} calls={trace.calls} />
          </div>
        )}
        <div className="msg__content">
          <TurnBody turn={turn} />
        </div>
        {stopped && <p className="hint">已停止。</p>}
        {finished && th.mode === "full" && <OutputChips turnId={turn.id} />}
        {!running && after}
        {deep.length > 0 && (
          <div className="deeplinks fade-in">
            <span className="deeplinks__label">{r?.gate === "confirm" ? "也可以在功能頁執行：" : "在功能頁打開："}</span>
            {deep.map((a) => (
              <Link key={a.label} to={a.to} className={`deeplink${a.primary ? " is-primary" : ""}`}>
                {a.label}
                <Icon name="chevronRight" strokeWidth={2} />
              </Link>
            ))}
          </div>
        )}
        {!running && <Actions turn={turn} isLast={isLast} />}
      </div>
    </div>
  );
}

function TurnBody({ turn }: { turn: Turn }) {
  const th = useThread();
  if (turn.archived) return <Archived turn={turn} />;
  if (turn.failure) return <Failure turn={turn} />;
  const r = turn.route;
  if (!r) return null;
  if (r.blocked) return <Blocked turn={turn} />;
  if (r.short_circuit)
    return (
      <div className="answer">
        <RichText text={r.short_circuit.reply} />
        <p className="answer__note">
          <Icon name="info" />第 2 段{r.short_circuit.by === "Jev" ? " Jev Choice" : "（地端規則）"}判為無關閒聊：快速短路回覆，沒有檢索、沒有呼叫 LLM
        </p>
      </div>
    );
  if (r.gate === "clarify")
    return (
      <div className="clarify">
        <p className="clarify__q">不太確定你想做哪一件事，請選一個（選了仍會重新經過第 1、2 段）：</p>
        <div className="choices">
          {r.options.map((o) => (
            <button key={o.intent} type="button" className="choice" onClick={() => th.ask(r.question, o.intent, turn.imageId)}>
              {o.label}
              <span className="choice__prob">{Math.round(o.prob * 100)}%</span>
            </button>
          ))}
        </div>
      </div>
    );
  if (r.photo?.kind === "unknown" && !r.question)
    return (
      <div className="answer">
        <p>這張照片在本機比對不到知識庫裡的畫作或工廠圖紙。可以補一句說明，例如「這是哪一幅畫？」或「這張圖紙的公差要求」；也可以在功能頁看辨識細節或拍照建檔。</p>
        {r.photo.domain === "art" && turn.imageId && (
          <div className="legacy">
            <PhotoStyleGuess imageId={turn.imageId} />
          </div>
        )}
      </div>
    );
  if (r.gate === "out_of_scope")
    return (
      <div className="answer">
        <p>這超出本系統的範圍。我可以：用文字或照片找畫、問畫作；查工廠圖紙與製程規範；查庫存、訂單、工單；把圖紙轉成 3D；執行生產排程；在你的權限內修改庫存、訂單、工單。</p>
      </div>
    );
  if (r.intent === "system") return <SystemCard />;
  if (r.intent === "batch_identify")
    return (
      <div className="answer">
        <p>批次辨識：一次選一批照片（最多 100 張），每張都用以圖搜圖同一套方法辨識；太模糊的會標出來請你重拍，結果可以匯出 CSV。照片在功能頁上一次選，按下方「打開批次辨識」。</p>
      </div>
    );
  const p = turn.part;
  if (!p) return null;
  switch (p.kind) {
    case "chat":
      return <ChatBody turn={turn} part={p} />;
    case "sql":
      return <SqlBody part={p} />;
    case "artSearch":
      return p.status === "loading" ? (
        <Pending label="以文搜畫中…" />
      ) : p.status === "error" ? (
        <ErrorNote code={p.error?.code} message={p.error?.message ?? "搜尋失敗"} requestId={p.error?.requestId} />
      ) : (
        <div className="answer">
          <p className="answer__lead">以文搜畫（Chinese-CLIP＋bge-m3）找到 {p.items.length} 幅{th.mode === "full" ? "，排序與比較在右側展示區" : "，前 3 名："}</p>
          {th.mode === "brief" && <Works items={p.items.slice(0, 3)} onAsk={(t) => th.ask(`介紹一下〈${t}〉`)} />}
        </div>
      );
    case "partSearch":
      return p.status === "loading" ? (
        <Pending label="搜尋圖紙中…" />
      ) : p.status === "error" ? (
        <ErrorNote code={p.error?.code} message={p.error?.message ?? "搜尋失敗"} requestId={p.error?.requestId} />
      ) : (
        <div className="answer">
          <p className="answer__lead">
            圖紙查找（bge-m3 檢索製程文件）{p.items.length ? `找到 ${p.items.length} 張` : "沒有找到符合的圖紙"}
            {p.hidden ? `・另有 ${p.hidden} 張不在你的資料範圍，檢索時就被濾掉` : ""}
          </p>
          {p.items.length > 0 && <DrawingWorks items={p.items.slice(0, 3)} onAsk={th.ask} />}
        </div>
      );
    case "reconstruct":
      return <ReconstructBody turn={turn} part={p} />;
    case "schedule":
      return <ScheduleBody turn={turn} part={p} />;
    case "change":
      return <ChangeBody turn={turn} part={p} />;
    case "compare":
      return <CompareBody part={p} />;
    default:
      return null;
  }
}

const Pending = ({ label }: { label: string }) => (
  <p className="pending">
    <Spinner />
    {label}
  </p>
);

function ErrorNote({ title = "沒有完成", message, code, requestId, onRetry }: { title?: string; message: string; code?: string; requestId?: string; onRetry?: () => void }) {
  return (
    <div className="panel-card alertcard" role="alert">
      <div className="panel-card__head">
        <span className="step-icon is-block">
          <Icon name="x" strokeWidth={3.2} />
        </span>
        <span className="panel-card__title">{title}</span>
        {code && <span className="panel-card__aside mono">{code}</span>}
      </div>
      <p className="panel-card__pad">{message}</p>
      {(requestId || onRetry) && (
        <div className="panel-card__foot">
          {requestId && <span className="panel-card__meta mono">request {requestId}</span>}
          {onRetry && (
            <button type="button" className="btn btn--secondary" onClick={onRetry}>
              重試
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** /agent/route 本身失敗：401 閘道拒絕、403、連不上、伺服器錯誤 */
function Failure({ turn }: { turn: Turn }) {
  const th = useThread();
  const f = turn.failure!;
  if (f.status === 401)
    return (
      <ErrorNote
        title="直接拒絕連線"
        code={`401 ${f.code}`}
        message={
          f.code === "TOKEN_INVALID"
            ? "憑證的簽章對不上（內容被改過或不是本系統簽發）。閘道在第 1 段就回 401，Jev、檢索與本地模型都沒有執行；這次嘗試已寫進拒絕並記錄。"
            : `沒有有效的身分憑證（${f.message}）。閘道在第 1 段就回 401，Jev、檢索與本地模型都沒有執行。`
        }
        requestId={f.requestId}
      />
    );
  if (f.status === 403) return <ErrorNote title="目前身分沒有權限" code={`403 ${f.code}`} message={f.message} requestId={f.requestId} />;
  if (f.status === 0)
    return <ErrorNote title="連不上伺服器" code={f.code} message={`${f.message}。請確認網路或後端是否啟動。`} onRetry={() => th.rerun(turn.id)} />;
  return <ErrorNote title="沒有完成" code={f.code} message={f.message} requestId={f.requestId} onRetry={() => th.rerun(turn.id)} />;
}

/** 第 1 段（角色授權）或第 2 段（Jev Choice）擋下；降級回應「查無資料」不透露文件存在、也不建議換誰 */
function Blocked({ turn }: { turn: Turn }) {
  const th = useThread();
  const switchAccount = useSwitchAccount();
  const [busy, setBusy] = useState(false);
  const [switchErr, setSwitchErr] = useState<string | null>(null);
  const r = turn.route!;
  const b = r.blocked!;
  if (b.degraded)
    return (
      <div className="panel-card">
        <div className="panel-card__head">
          <Icon name="lock" />
          <span className="panel-card__title">{b.reason}</span>
        </div>
        <p className="panel-card__pad panel-card__meta">
          降級回應：只看你目前憑證權限內的資料，不透露其他文件是否存在。已記錄 <span className="mono">{b.log_no}</span>
        </p>
      </div>
    );
  const checks = (b.stage === 1 ? r.auth.checks : (r.guard?.checks ?? [])).filter((c) => c.ok === false);
  const retry = b.stage === 1 ? r.auth.retry : null;
  return (
    <div className="panel-card alertcard">
      <div className="panel-card__head">
        <span className="step-icon is-block">
          <Icon name="x" strokeWidth={3.2} />
        </span>
        <span className="panel-card__title">{b.stage === 1 ? "第 1 段：角色授權沒有通過" : `第 2 段：Jev Choice 擋下了這個請求（${b.rule}）`}</span>
        <span className="panel-card__aside">已拒絕並記錄</span>
      </div>
      <p className="panel-card__pad">{b.stage === 1 ? "這個請求沒有送給 Jev，也沒有碰到任何資料。" : "它沒有進入檢索，也沒有碰到任何資料與模型。"}</p>
      <ul className="rows">
        {checks.map((c) => (
          <li key={c.key} className="rows__item is-block">
            <span className="checks__dot" />
            <span className="rows__label">{c.label}</span>
            <span className="rows__main">
              <small>{c.by}・</small>
              {c.detail}
            </span>
          </li>
        ))}
      </ul>
      <div className="panel-card__foot">
        <span className="panel-card__meta">
          紀錄 <span className="mono">{b.log_no}</span>・判斷：{b.judge}
          {switchErr && <span className="is-error">・{switchErr}</span>}
        </span>
        {retry && (
          <button
            type="button"
            className="btn btn--secondary"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setSwitchErr(null);
              try {
                await switchAccount(retry.account_id);
                th.ask(r.question, turn.forced, turn.imageId);
              } catch (e) {
                // 有寫入還沒收到結果時不能切換（見 api/writes.ts）
                setSwitchErr((e as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "切換中…" : `切換成〈${retry.label}〉再試一次`}
          </button>
        )}
      </div>
    </div>
  );
}

/** 從瀏覽器紀錄還原的一輪：只有公開內容；工廠內部資料要以目前身分重新查詢 */
function Archived({ turn }: { turn: Turn }) {
  const th = useThread();
  const a = turn.archived!;
  const [cite, setCite] = useState<number | null>(null);
  return (
    <div className="answer">
      {a.artworkId && th.mode === "brief" && <Subject kind="artwork" id={a.artworkId} title={a.artworkLabel ?? a.artworkId} meta="作品・知識庫" />}
      {a.answer && <RichText text={a.answer} activeRef={cite} onCite={setCite} />}
      {a.sources && a.sources.length > 0 && <Sources sources={a.sources} active={cite} onActive={setCite} />}
      {a.artResults && th.mode === "brief" && <Works items={a.artResults.slice(0, 3)} onAsk={(t) => th.ask(`介紹一下〈${t}〉`)} />}
      {a.outcome === "unconfirmed" ? (
        <p className="hint">
          <Icon name="info" />
          {a.summary}（<Link to="/approvals">核准紀錄</Link>）
        </p>
      ) : a.outcome && REJECTED[a.outcome] ? (
        <p className="hint">
          <Icon name="lock" />
          {a.summary}・被關卡拒絕的提問不保存內容
        </p>
      ) : a.redacted && canRerun(turn) && (
        <div className="callout">
          <Icon name="lock" />
          <span>這一輪含工廠內部或非公開資料，沒有存進瀏覽器（也不會留給下一個使用這台電腦的人）。</span>
          <button type="button" className="link-btn" onClick={() => th.rerun(turn.id)}>
            以目前身分重新查詢
            <Icon name="chevronRight" strokeWidth={2} />
          </button>
        </div>
      )}
      {a.outcome !== "unconfirmed" && !(a.outcome && REJECTED[a.outcome]) && (!a.redacted || !canRerun(turn)) && !a.answer && !a.artResults && a.outcome !== "pass" && <p className="hint">{a.summary}</p>}
    </div>
  );
}

function Subject({ kind, id, title, meta }: { kind: "part" | "artwork"; id: string; title: string; meta: string }) {
  const src = kind === "part" ? `/api/v1/parts/${encodeURIComponent(id)}/drawing?size=thumb` : `/api/v1/artworks/${encodeURIComponent(id)}/image?size=thumb`;
  return (
    <div className={`subject${kind === "part" ? " subject--drawing" : ""}`}>
      <div className="subject__media">
        <img src={assetUrl(src)} alt="" loading="lazy" />
      </div>
      <div className="subject__body">
        <p className="subject__title">{title}</p>
        <p className="subject__meta">{meta}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 問答（第 3～7 段）

function ChatBody({ turn, part: p }: { turn: Turn; part: ChatPart }) {
  const th = useThread();
  const [cite, setCite] = useState<number | null>(null);
  const r = turn.route!;
  const d = dispatchOf(r);
  const vis = visiblePart(turn);
  const pf = p.sources?.post_filter;
  return (
    <div className="answer">
      {th.mode === "brief" && p.target.artwork_id && <Subject kind="artwork" id={p.target.artwork_id} title={d.artwork_label ?? p.target.artwork_id} meta="作品・知識庫" />}
      {th.mode === "brief" && vis && <Subject kind="part" id={vis.id} title={vis.label} meta={`圖紙・${vis.level}`} />}
      {p.status === "retrieving" && <Pending label="檢索知識庫中…（第 3 段 Metadata Filter）" />}
      {p.status === "streaming" && !p.text && <Pending label={`已取回 ${p.sources?.sources.length ?? 0} 段資料，第 4～6 段驗證後等待本地模型回答…`} />}
      {pf && !(pf.gate && !pf.gate.passed) && (pf.flagged.length > 0 || th.mode === "full") && (
        <p className={`answer__note${pf.flagged.length ? " is-warn" : ""}`}>
          <Icon name={pf.flagged.length ? "shield" : "layers"} />
          上下文 {pf.kept} 段（從 {pf.candidates} 段候選經{pf.engine === "jev" ? " Jev Noul 驗證、Jev Score 重排" : "地端驗證、重排"}）
          {pf.flagged.length ? `；已剔除 ${pf.flagged.length} 段有洩密風險的段落` : ""}
          {p.target.part_id ? "；機密圖紙只用地端模型" : ""}
        </p>
      )}
      {p.text && <RichText text={p.text.trim()} streaming={p.status === "streaming"} activeRef={cite} onCite={setCite} />}
      {p.sources?.conflict_check?.conflict && p.text && (
        <p className="answer__note is-warn">
          <Icon name="info" />
          參考資料的說法不一致（{p.sources.conflict_check.refs.map((n) => `[${n}]`).join("")}），回答已指出差異。
        </p>
      )}
      {p.error && (
        <ErrorNote
          title={
            p.error.code === "NOT_IN_KB"
              ? "知識庫中沒有這份資料"
              : p.error.code === "DATA_SCOPE_DENIED"
                ? "目前身分看不到這份資料"
                : p.error.code === "STRATEGY_UNAVAILABLE"
                  ? "生成端目前無法使用"
                  : "回答失敗"
          }
          message={p.error.message}
          code={p.error.code}
          requestId={p.error.request_id}
          onRetry={() => th.rerun(turn.id)}
        />
      )}
      {p.done?.degraded && (
        <p className="answer__note">
          <Icon name="lock" />第 6 段生成閘門判斷權限內沒有可答的內容，回覆「查無資料」，沒有呼叫 LLM
        </p>
      )}
      {p.done && !p.done.degraded && th.mode === "full" && (
        <p className="answer__meta num">
          {p.done.fallback ? "本地備援模型・" : ""}
          {STRATEGY_LABEL[p.done.strategy_used] ?? p.done.strategy_used}・{p.done.model}・首字 {seconds(p.done.latency_ms.first_token)}・總計 {seconds(p.done.latency_ms.total)}
        </p>
      )}
      {p.sources && p.sources.sources.length > 0 && p.text && <Sources sources={p.sources.sources} active={cite} onActive={setCite} />}
    </div>
  );
}

function Sources({ sources, active, onActive }: { sources: SourceItem[]; active: number | null; onActive: (n: number | null) => void }) {
  const [open, setOpen] = useState(false);
  const refs = useRef<Record<number, HTMLLIElement | null>>({});
  useEffect(() => {
    if (active == null) return;
    setOpen(true);
    requestAnimationFrame(() => refs.current[active]?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  }, [active]);
  return (
    <div className={`sources${open ? " is-open" : ""}`}>
      <button type="button" className="sources__toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="sources__label">{sources.length} 個來源</span>
        <span className="sources__topics">{sources.map((s) => s.topic).join("・")}</span>
        <Icon name="chevronDown" strokeWidth={2} />
      </button>
      {open && (
        <ol className="sources__list">
          {sources.map((s) => (
            <li
              key={s.chunk_id}
              ref={(el) => {
                refs.current[s.ref] = el;
              }}
              onClick={() => onActive(s.ref)}
              className={`source${active === s.ref ? " is-active" : ""}`}
            >
              <p className="source__head">
                <span className="source__n">{s.ref}</span>
                <span className="source__title">
                  {s.title ?? s.artwork_title}・{s.topic}
                </span>
                <span className="source__score">相似度 {s.score.toFixed(2)}</span>
              </p>
              <p className="source__text">{s.text}</p>
              <p className="source__from">
                {s.source_url ? (
                  <a href={s.source_url} target="_blank" rel="noreferrer">
                    出處・{s.license}
                  </a>
                ) : (
                  `出處：${s.source_label ?? "—"}・${s.license}`
                )}
                {s.level && s.level !== "公開" ? `・${s.level}` : ""}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Works({ items, onAsk }: { items: { artwork: { id: string; title_zh: string; artist_zh: string; date_text: string; thumb_url: string }; score: number }[]; onAsk: (title: string) => void }) {
  return (
    <div className="works">
      {items.map(({ artwork: a, score }, i) => (
        <figure key={a.id} className={`work${i === 0 ? " is-best" : ""}`}>
          <button type="button" className="work__media" onClick={() => onAsk(a.title_zh)}>
            <div className="plinth">
              <img src={assetUrl(a.thumb_url)} alt={a.title_zh} loading="lazy" />
            </div>
          </button>
          <figcaption className="work__caption">
            <span className="work__flag">{i === 0 ? "最符合" : `分數 ${score.toFixed(2)}`}</span>
            <span className="work__title">{a.title_zh}</span>
            <span className="work__meta">
              {a.artist_zh}・{short(a.date_text)}
            </span>
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

function DrawingWorks({ items, onAsk }: { items: { part: { id: string; name_zh: string; part_no: string; drawing_no: string; revision: string; confidentiality: string; thumb_url: string } }[]; onAsk: (q: string) => void }) {
  return (
    <div className="works works--drawings">
      {items.map(({ part: p }) => (
        <figure key={p.id} className="work">
          <div className="work__media">
            <div className="plinth plinth--drawing">
              <img src={assetUrl(p.thumb_url)} alt={p.name_zh} loading="lazy" />
            </div>
          </div>
          <figcaption className="work__caption">
            <span className={`work__flag${p.confidentiality === "機密" ? " is-secret" : ""}`}>{p.confidentiality}</span>
            <span className="work__title">{p.name_zh}</span>
            <span className="work__meta">
              {p.part_no}・{p.drawing_no} rev.{p.revision}
            </span>
            <span className="work__actions">
              <button type="button" className="link-btn" onClick={() => onAsk(`${p.name_zh}有哪些公差要求？`)}>
                問規格
                <Icon name="chevronRight" strokeWidth={2} />
              </button>
              <button type="button" className="link-btn" onClick={() => onAsk(`把${p.name_zh}轉成 3D`)}>
                轉成 3D
                <Icon name="chevronRight" strokeWidth={2} />
              </button>
            </span>
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- 庫存 Text-to-SQL

const cleanSql = (text: string) => text.replace(/```(?:sql|sqlite)?\n?/gi, "").trim();

function SqlBody({ part: p }: { part: SqlPart }) {
  const th = useThread();
  const [showSql, setShowSql] = useState(false);
  const last = p.attempts.at(-1);
  const status =
    p.status === "generating"
      ? `本地模型寫 SQL 中${p.attempts.length ? `（第 ${p.attempts.length + 1} 次，上一次：${p.attempts.at(-1)?.error ?? ""}）` : ""}…`
      : p.status === "executing"
        ? "唯讀檢查、執行中…"
        : p.status === "answering" && !p.answer
          ? "依查詢結果回答中…"
          : null;
  return (
    <div className="answer">
      {status && <Pending label={status} />}
      {p.answer && <RichText text={p.answer.trim()} streaming={p.status === "answering"} />}
      {p.result && (
        <p className="answer__meta num">
          {p.result.row_count} 筆{p.result.truncated ? "（已截斷）" : ""}・唯讀・{p.result.exec_ms} ms{th.mode === "brief" ? "・結果表與圖表在工廠模組" : "・結果表在右側展示區"}
        </p>
      )}
      {th.mode === "full" && (last || p.draft) && (
        <>
          <button type="button" className="link-btn" onClick={() => setShowSql((s) => !s)}>
            <Icon name="code" />
            {showSql ? "隱藏查詢語法" : "查詢語法"}
          </button>
          {showSql && <pre className="code code--block">{cleanSql(last?.sql ?? p.draft)}</pre>}
        </>
      )}
      {p.error && <ErrorNote title={p.error.code === "SQL_REJECTED" ? "這個查詢被唯讀檢查擋下" : "查詢失敗"} message={p.error.message} code={p.error.code} requestId={p.error.request_id} />}
    </div>
  );
}

// ---------------------------------------------------------------- 3D 重建、排程（使用者確認後才執行）

function Specs({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="specs">
      {items.map(([k, v]) => (
        <div key={k} className="specs__item">
          <dd className="specs__value">{v}</dd>
          <dt className="specs__label">{k}</dt>
        </div>
      ))}
    </dl>
  );
}

const CAD_STAGES = ["前處理、辨識、讀尺寸", "Ortho2CAD 產生 CadQuery 程式", "沙箱執行並輸出 STEP／STL"];

function ReconstructBody({ turn, part: p }: { turn: Turn; part: ReconstructPart }) {
  const th = useThread();
  const vis = visiblePart(turn);
  const [showCode, setShowCode] = useState(false);
  const source = p.imageId ? "你附的照片" : vis ? `〈${vis.label}〉的三視圖` : null;
  if (th.mode === "brief")
    return (
      <div className="answer">
        {vis && <Subject kind="part" id={vis.id} title={vis.label} meta={`圖紙・${vis.level}`} />}
        <RichText
          text={
            source
              ? `可以把${source}轉成 **3D 模型**。約 1–2 分鐘、會載入約 6 GB 的模型，全程在本機；進入工廠模組按「開始轉換」後，模型會顯示在右側展示區，可以旋轉、縮放。`
              : "這句話交給 Ortho2CAD 3D 重建，但還不知道要轉哪一張圖紙：可以附一張圖紙照片再說一次。"
          }
        />
      </div>
    );
  const j = p.job;
  const idx = !j ? -1 : j.status === "preparing" ? 0 : j.status === "generating" ? 1 : j.status === "executing" ? 2 : 3;
  const res = j?.result;
  return (
    <div className={`panel-card taskcard is-${j ? j.status : p.cancelled ? "cancelled" : "confirm"}`}>
      <div className="taskcard__head">
        {vis ? (
          <span className="taskcard__thumb plinth plinth--drawing">
            <img src={assetUrl(`/api/v1/parts/${encodeURIComponent(vis.id)}/drawing?size=thumb`)} alt="" />
          </span>
        ) : p.imageId ? (
          <span className="taskcard__thumb plinth plinth--drawing">
            <img src={api.uploadedImageUrl(p.imageId)} alt="" />
          </span>
        ) : (
          <span className="taskcard__icon">
            <Icon name="cube" />
          </span>
        )}
        <div className="taskcard__body">
          <p className="taskcard__title">{source ? `把${source}轉成 3D` : "3D 重建"}</p>
          <p className="taskcard__desc">Ortho2CAD 把三視圖轉成 CadQuery 程式，再產生 STEP／STL；約 1–2 分鐘、載入約 6 GB 模型（記憶體不足時先釋放其他模型），所以不自動執行。</p>
        </div>
      </div>
      {!j && !p.cancelled && source && (
        <div className="taskcard__actions">
          <button type="button" className="btn btn--primary" onClick={() => th.startReconstruct(turn.id)}>
            開始轉換
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => th.cancelTask(turn.id)}>
            取消
          </button>
        </div>
      )}
      {!j && !source && <p className="taskcard__note">附一張圖紙照片，或說出圖紙名稱再問一次。</p>}
      {p.cancelled && !j && <p className="taskcard__note">已取消，沒有載入模型。</p>}
      {j && (j.status === "preparing" || j.status === "generating" || j.status === "executing") && (
        <div className="taskcard__run">
          <ol className="stagelist">
            {CAD_STAGES.map((s, i) => (
              <li key={s} className={`stagelist__item ${i < idx ? "is-done" : i === idx ? "is-doing" : "is-todo"}`}>
                {i < idx ? (
                  <span className="step-icon is-ok">
                    <Icon name="check" strokeWidth={3.2} />
                  </span>
                ) : i === idx ? (
                  <Spinner />
                ) : (
                  <span className="step-icon is-todo" />
                )}
                <span>{s}</span>
                {i === 1 && i <= idx && <span className="num muted">{j.code.length} 字</span>}
              </li>
            ))}
          </ol>
          <button type="button" className="btn btn--ghost" onClick={() => th.cancelTask(turn.id)}>
            停止
          </button>
        </div>
      )}
      {j?.status === "stopped" && <p className="taskcard__note">已停止。</p>}
      {j?.status === "error" && j.error && <ErrorNote title="3D 重建失敗" message={j.error.message} code={j.error.code} requestId={j.error.request_id} />}
      {j?.status === "done" && res && (
        <div className="taskcard__result fade-in">
          {res.ok ? (
            <Specs
              items={[
                ["外框尺寸 mm", res.dims ? `${Math.round(res.dims.width)}×${Math.round(res.dims.depth)}×${Math.round(res.dims.height)}` : "—"],
                ["面數", res.faces ?? "—"],
                ["與標準模型 IoU", res.iou != null ? res.iou.toFixed(2) : "未收錄"],
                ["總耗時", j.done ? seconds(j.done.latency_ms.total) : "—"],
              ]}
            />
          ) : (
            <ErrorNote title="模型產生的程式沒有通過驗證" message={res.error ?? "沒有產生可用的實體"} code={res.code} />
          )}
          <div className="taskcard__foot">
            {res.files["model.stl"] && (
              <a className="link-btn" href={assetUrl(res.files["model.stl"])} download>
                下載 STL
                <Icon name="chevronRight" strokeWidth={2} />
              </a>
            )}
            {j.code && (
              <button type="button" className="link-btn" onClick={() => setShowCode((s) => !s)}>
                {showCode ? "隱藏程式碼" : "CadQuery 程式碼"}
                <Icon name="chevronRight" strokeWidth={2} />
              </button>
            )}
            <span className="taskcard__meta">全程在本機・外送 {j.done?.egress.bytes ?? 0} B</span>
          </div>
          {showCode && <pre className="code code--block code--scroll">{j.code}</pre>}
        </div>
      )}
    </div>
  );
}

function ScheduleBody({ turn, part: p }: { turn: Turn; part: SchedulePart }) {
  const th = useThread();
  const j = p.job;
  if (th.mode === "brief")
    return (
      <div className="answer">
        <p>這句話交給 Timefold 生產排程：把所有未完工工單重新排到各機台，求解約 20 秒，結果會寫回資料庫，所以不自動執行。進入工廠模組確認後，新的甘特圖會大張顯示在右側。</p>
      </div>
    );
  const k = j?.progress?.kpis ?? j?.solution?.kpis;
  return (
    <div className={`panel-card taskcard is-${j ? j.status : p.cancelled ? "cancelled" : "confirm"}`}>
      <div className="taskcard__head">
        <span className="taskcard__icon">
          <Icon name="calendar" />
        </span>
        <div className="taskcard__body">
          <p className="taskcard__title">重新排程所有未完工工單</p>
          <p className="taskcard__desc">Timefold 求解約 20 秒，結果寫回資料庫；目前的排程已顯示在右側展示區。</p>
        </div>
      </div>
      {!j && !p.cancelled && (
        <div className="taskcard__actions">
          <button type="button" className="btn btn--primary" onClick={() => th.startSchedule(turn.id)}>
            開始排程
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => th.cancelTask(turn.id)}>
            取消
          </button>
        </div>
      )}
      {p.cancelled && !j && <p className="taskcard__note">已取消。</p>}
      {j && (j.status === "starting" || j.status === "solving") && (
        <div className="taskcard__run">
          <p className="taskcard__stats num">
            <span>
              <Spinner /> {j.meta?.engine_label ?? "排程"}計算中 {(j.elapsedMs / 1000).toFixed(1)} / {j.meta?.seconds ?? "—"} 秒
            </span>
            {j.progress && (
              <>
                <span>
                  硬限制 <b className={j.progress.hard < 0 ? "is-bad" : "is-good"}>{j.progress.hard}</b>
                </span>
                <span>
                  軟限制 <b>{j.progress.soft.toLocaleString()}</b>
                </span>
              </>
            )}
          </p>
          {j.meta?.fallback_reason && <p className="hint">Timefold 連不上，改用簡易排程：{j.meta.fallback_reason}</p>}
          <button type="button" className="btn btn--ghost" onClick={() => th.cancelTask(turn.id)}>
            停止
          </button>
        </div>
      )}
      {j?.status === "stopped" && <p className="taskcard__note">已停止。</p>}
      {j?.status === "error" && j.error && <ErrorNote title="排程失敗" message={j.error.message} code={j.error.code} requestId={j.error.request_id} />}
      {j?.status === "done" && k && (
        <div className="taskcard__result fade-in">
          <Specs
            items={[
              ["工單", k.n_work_orders],
              ["準時", `${k.n_work_orders - k.n_late}／${k.n_work_orders}`],
              ["會延遲", `${k.n_late} 張`],
              ["分數", j.solution?.score ?? "—"],
            ]}
          />
          <p className="panel-card__foot panel-card__meta">
            結果已寫回（{j.solution?.run_id}）・{j.meta?.engine_label}・求解 {seconds(j.done?.latency_ms.solve ?? null)}
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 修改資料

function ChangeBody({ turn, part: p }: { turn: Turn; part: ChangePart }) {
  const th = useThread();
  const [note, setNote] = useState("");
  const pv = p.preview;
  if (!pv) return p.error ? <ErrorNote title="試算失敗" message={p.error} /> : <Pending label="抽取參數、權限判定、試算中…" />;
  if (th.mode === "brief")
    return (
      <div className="answer">
        <RichText
          text={
            pv.next === "rejected"
              ? `這筆修改**不能執行**：${pv.message}`
              : pv.next === "approval"
                ? `我試算好了「${pv.op_label}」，但超過額度，需要主管核准。進入工廠模組核對數字、補一句說明送主管核准。`
                : pv.next === "need_info"
                  ? pv.message
                  : `我試算好了「${pv.op_label}」，檢查都通過。進入工廠模組核對修改前後的數字，**確認後才會寫入**。`
          }
        />
      </div>
    );
  // 結果未確認的寫入也不再給按鈕：伺服器可能已經完成，再按一次可能變成第二筆
  const finished = p.committed || p.approval || p.status === "unconfirmed";
  return (
    <div className="panel-card changecard">
      <div className="panel-card__head">
        <Icon name="edit" />
        <span className="panel-card__title">修改資料・{pv.op_label}</span>
        <span className="panel-card__aside">
          以「{pv.account.label}」身分・{pv.latency_ms ?? 0} ms
        </span>
      </div>
      {Object.keys(pv.param_labels).length > 0 && (
        <p className="panel-card__pad changecard__params">
          {Object.entries(pv.param_labels).map(([k, v]) => (
            <span key={k}>
              <small>{k}</small> <b>{String(v)}</b>
            </span>
          ))}
        </p>
      )}
      {pv.checks.length > 0 && (
        <ul className="rows">
          {pv.checks.map((c) => (
            <li key={c.key} className={`rows__item is-${c.ok === false ? "block" : c.ok ? "ok" : "skip"}`}>
              <span className="checks__dot" />
              <span className="rows__label">{c.label}</span>
              <span className="rows__main">{c.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {pv.diff.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>資料</th>
                <th>欄位</th>
                <th className="num">修改前</th>
                <th className="num">修改後</th>
              </tr>
            </thead>
            <tbody>
              {pv.diff.map((r, i) => (
                <tr key={i}>
                  <td>{r.label}</td>
                  <td>{r.field}</td>
                  <td className="num">{r.before == null ? "（無）" : String(r.before)}</td>
                  <td className="num">
                    <b>{r.after == null ? "（無）" : String(r.after)}</b>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="panel-card__pad">
        {pv.next === "rejected" && (
          <p className="outcome is-block">
            <b>已拒絕，沒有任何資料被修改。</b> {pv.message}（已記進稽核紀錄）
          </p>
        )}
        {pv.next === "need_info" && <p className="outcome is-warn">{pv.message}</p>}
        {pv.next === "confirm" && !finished && (
          <div className="taskcard__actions">
            <span className="outcome is-ok">{pv.message}</span>
            <button type="button" className="btn btn--primary" disabled={p.status === "committing"} onClick={() => th.commitChange(turn.id, null)}>
              {p.status === "committing" ? "寫入中…" : "確認寫入"}
            </button>
          </div>
        )}
        {pv.next === "approval" && !finished && (
          <div className="changecard__approval">
            <p className="outcome is-warn">超過額度，需要主管核准：{pv.reasons.join("；")}</p>
            <div className="taskcard__actions">
              <input className="mfield" value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} placeholder="申請說明（選填，例如原因）" aria-label="申請說明" />
              <button type="button" className="btn btn--primary" disabled={p.status === "committing"} onClick={() => th.commitChange(turn.id, note)}>
                {p.status === "committing" ? "送出中…" : "送主管核准"}
              </button>
            </div>
          </div>
        )}
        {p.committed && (
          <p className="outcome is-ok">
            <b>✓ {p.committed.text}</b> 回覆依資料庫讀回的實際結果產生；異動單與稽核紀錄已寫入。
          </p>
        )}
        {p.approval && (
          <p className="outcome is-warn">
            已建立待核准單 <span className="mono">{p.approval.ap_no}</span>，等主管核准。切換成「主管」到 <Link to="/approvals">待核准清單</Link> 處理；核准時會重新試算。
          </p>
        )}
        {p.status === "unconfirmed" ? (
          <p className="outcome is-warn">
            <b>結果未確認</b>：{p.error}。{writeUnconfirmed(p.action)}（<Link to="/approvals">核准紀錄</Link>、<Link to="/inventory">庫存・工單</Link>）。
          </p>
        ) : (
          p.error && <p className="outcome is-block">{p.error}</p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 並排比較、系統狀態

const NAME_KEYS = new Set(["title", "title_en", "name", "part_no", "drawing_no", "topics"]);

function CompareBody({ part: p }: { part: ComparePart }) {
  if (p.status === "single")
    return (
      <div className="answer">
        <p>{p.labels[0] ? `要拿〈${p.labels[0]}〉和哪一件比？到比較頁選另一件。` : "請說出兩幅畫或兩張圖紙的名稱（例如「比較連接法蘭和軸承座」），或到比較頁選。"}</p>
      </div>
    );
  if (p.status === "loading") return <Pending label="讀取兩件的資料…" />;
  if (p.status === "error" || !p.data) return <ErrorNote message={p.error?.message ?? "讀取失敗"} code={p.error?.code} requestId={p.error?.requestId} />;
  const data = p.data;
  const name = (k: "a" | "b") => String(data.kind === "artwork" ? data[k].title_zh : data[k].name_zh);
  const diffs = data.rows.filter((r) => !r.same && (r.a || r.b) && !NAME_KEYS.has(r.key));
  return (
    <div className="answer">
      <p>
        〈{name("a")}〉和〈{name("b")}〉有 <b>{data.differences}</b> 個欄位不同{diffs.length > 6 && "，先列前 6 個"}：
      </p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>欄位</th>
              <th>〈{name("a")}〉</th>
              <th>〈{name("b")}〉</th>
            </tr>
          </thead>
          <tbody>
            {diffs.slice(0, 6).map((r) => (
              <tr key={r.key}>
                <td>{r.label}</td>
                <td>{r.a ?? "—"}</td>
                <td>{r.b ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="answer__meta">直接讀知識庫，不經生成・每格出處在比較頁</p>
    </div>
  );
}

function SystemCard() {
  const { data } = useStatus();
  if (!data) return <Pending label="讀取系統狀態…" />;
  const m = data.memory;
  return (
    <div className="panel-card">
      <div className="panel-card__head">
        <span className="panel-card__title">系統狀態</span>
        <span className="panel-card__aside">{data.status === "ok" ? "服務正常" : data.outage_simulated ? "推論伺服器離線（模擬）" : "部分服務異常"}</span>
      </div>
      {m && (
        <>
          <div className="panel-card__pad">
            <div className="meter">
              <div className="meter__fill" style={{ width: `${m.percent}%` }} />
              <div className="meter__mark is-warn" style={{ left: `${m.threshold}%` }} />
            </div>
            <p className="hint num">
              系統記憶體 {Math.round(m.percent)}%（超過 {m.threshold}% 會釋放目前流程用不到的模型）
              {m.gpu ? `・顯示記憶體 ${Math.round(m.gpu.percent)}%` : ""}
            </p>
          </div>
          <ul className="rows">
            {m.models.map((x) => (
              <li key={x.label} className="rows__item">
                <span className={`status-dot ${x.loaded ? "is-ok" : "is-off"}`} />
                <span className="rows__main">{x.label}</span>
                <span className="rows__aside">{x.loaded ? "已載入" : "未載入"}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 成果標籤、動作

/** 模組裡：這一輪產生的成果，點了在右側展示區顯示 */
function OutputChips({ turnId }: { turnId: string }) {
  const th = useThread();
  const outs = th.outputsOf(turnId);
  if (!outs.length) return null;
  return (
    <div className="outchips fade-in">
      {outs.map((o) => (
        <OutputChip key={o.key} o={o} on={th.activeOutput === o.key} onClick={() => th.showOutput(o.key)} />
      ))}
    </div>
  );
}

export function OutputChip({ o, on, onClick }: { o: Output; on: boolean; onClick: () => void }) {
  const img = thumbOf(o);
  const pic = o.kind === "drawing" || o.kind === "artwork" || o.kind === "detail" || o.kind === "similar";
  return (
    <button type="button" className={`outchip outchip--${o.kind}${on ? " is-on" : ""}`} onClick={onClick} title="在右側展示區顯示">
      <span className="outchip__thumb">
        {pic && img ? (
          <img src={assetUrl(img)} alt="" />
        ) : (
          <Icon name={o.kind === "model" ? "cube" : o.kind === "schedule" ? "calendar" : o.kind === "timeline" ? "layers" : "table"} />
        )}
      </span>
      <span className="outchip__text">
        <span className="outchip__kind">{KIND_LABEL[o.kind]}</span>
        <span className="outchip__title">{o.title}</span>
      </span>
      <span className="outchip__state">{on ? "顯示中" : "在右側顯示"}</span>
    </button>
  );
}

function Actions({ turn, isLast }: { turn: Turn; isLast: boolean }) {
  const th = useThread();
  const [copied, setCopied] = useState(false);
  const p = turn.part;
  const text = p?.kind === "chat" ? p.text : p?.kind === "sql" ? p.answer : (turn.archived?.answer ?? "");
  return (
    <div className="msg__actions">
      {text && (
        <button
          type="button"
          className="icon-btn"
          title="複製"
          aria-label="複製回答"
          onClick={() => {
            void navigator.clipboard?.writeText(text.replace(/\s?\[(\d+|畫面)\]/g, ""));
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
        >
          <Icon name={copied ? "check" : "copy"} />
        </button>
      )}
      {isLast && !turn.archived && canRerun(turn) && (
        <button type="button" className="icon-btn" title="重新產生" aria-label="重新產生" onClick={th.regenerate}>
          <Icon name="refresh" />
        </button>
      )}
      <span className="msg__meta">{turn.route?.intent_label ?? turn.archived?.intentLabel ?? ""}</span>
    </div>
  );
}
