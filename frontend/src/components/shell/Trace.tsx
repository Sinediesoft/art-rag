import { useState } from "react";
import type { RouteResponse } from "../../api/client";
import type { GuardCheck, JevCallInfo } from "../../api/sse";
import type { Stage, StageState } from "../../shell/stages";
import { Icon, Spinner } from "./Icons";

/**
 * 來源：ArtRAG-前端demo/source/src/components/Trace.tsx 的版面（收合一行＋展開每一段）。
 * 內容換成七段權限控管（ADR 015／030）：認證與授權 → Jev Choice → Metadata Filter → Jev Noul → Jev Score → 生成閘門 → 本地 LLM。
 * 每一段的狀態由 shell/stages.ts 依 /agent/route 與 SSE（含後端 pipeline 軌跡）推出；這裡只負責畫。
 */
const kb = (b: number) => `${(b / 1024).toFixed(1)} KB`;

export function TraceLine({
  stages,
  summary,
  egress,
  running,
  stopped,
  blocked,
  open,
  onToggle,
}: {
  stages: { key: string; short: string; state: StageState | string; title?: string }[];
  summary: string | null;
  egress: number | null;
  running: boolean;
  stopped: boolean;
  blocked: boolean;
  open: boolean;
  onToggle?: () => void;
}) {
  const doing = stages.find((s) => s.state === "doing");
  const status = running ? "running" : blocked ? "blocked" : stages.some((s) => s.state === "warn") ? "warned" : "passed";
  const body = (
    <>
      <ol className="trace__stages" aria-label="七段處理軌跡">
        {stages.map((s) => (
          <li key={s.key} className={`trace__stage is-${s.state}`} title={s.title ?? s.short} data-state={s.state}>
            <span className="trace__dot">{s.state === "doing" ? <Spinner /> : null}</span>
            <span className="trace__label">{s.short}</span>
          </li>
        ))}
      </ol>
      <span className="trace__summary">
        {running ? (
          <span className="shimmer">{doing ? `${doing.short}…` : "第 1 段認證與授權、第 2 段 Jev Choice…"}</span>
        ) : stopped ? (
          "已停止"
        ) : (
          <>
            <span className="trace__result">{summary}</span>
            {egress !== null && (
              <span
                className={`trace__meta trace__egress${egress ? " is-out" : ""}`}
                title={egress ? "只送遮蔽個資、代號化後的文字與公開段落；照片、原始名稱、機密段落都不送" : "全程在本機"}
              >
                {egress ? `外送 ${kb(egress)} → Jev・已代號化` : "外送 0 B・全程地端"}
              </span>
            )}
          </>
        )}
      </span>
      {onToggle && (
        <span className="trace__toggle">
          處理細節
          <Icon name="chevronDown" strokeWidth={2} />
        </span>
      )}
    </>
  );
  if (!onToggle)
    return (
      <div className={`trace is-${status}`} role="group" aria-label="處理軌跡">
        {body}
      </div>
    );
  return (
    <button type="button" className={`trace is-${status}${open ? " is-open" : ""}`} onClick={onToggle} aria-expanded={open}>
      {body}
    </button>
  );
}

function StepIcon({ state }: { state: StageState }) {
  if (state === "doing") return <Spinner />;
  return (
    <span className={`step-icon is-${state}`}>
      {state === "ok" && <Icon name="check" strokeWidth={3.2} />}
      {state === "block" && <Icon name="x" strokeWidth={3.2} />}
      {state === "warn" && "!"}
      {state === "handoff" && <Icon name="arrowRight" strokeWidth={3} />}
    </span>
  );
}

function Checks({ checks }: { checks: GuardCheck[] }) {
  return (
    <ul className="checks">
      {checks.map((c) => {
        const st = c.ok === false ? "block" : c.warn ? "warn" : c.ok ? "ok" : "skip";
        return (
          <li key={c.key} className={`checks__item is-${st}`}>
            <span className="checks__dot" />
            <span className="checks__label">{c.label}</span>
            <span className="checks__detail">
              <span className="checks__by">{c.by === "Jev" ? "雲端 Jev" : "地端"}・</span>
              {c.detail}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

const SENT_LABEL: Record<number, string> = {
  2: "Jev Choice・使用者的話",
  4: "Jev Noul・公開段落（雙重驗證）",
  5: "Jev Score・通過驗證的公開段落（評分）",
  6: "Jev Noul・留下的段落與作品資料（生成閘門）",
};

/** 展開後：每一段做了什麼；技術細節（JWT payload、意圖機率、送給 Jev 的內容）再收一層 */
export function TraceDetail({ stages, route, calls }: { stages: Stage[]; route: RouteResponse | null; calls: JevCallInfo[] }) {
  const [tech, setTech] = useState(false);
  return (
    <div className="trace-detail">
      <ol className="steps">
        {stages.map((s) => (
          <li key={s.key} className={`steps__item is-${s.state}`}>
            <StepIcon state={s.state} />
            <div className="steps__body">
              <p className="steps__title">
                {s.title}
                <small>{STATE_TEXT[s.state]}</small>
              </p>
              <p className="steps__detail">{s.detail}</p>
              {s.checks && s.checks.length > 0 && s.state !== "todo" && s.state !== "doing" && <Checks checks={s.checks} />}
            </div>
          </li>
        ))}
      </ol>
      {route && (
        <>
          <button type="button" className="link-btn trace-detail__more" onClick={() => setTech((x) => !x)}>
            {tech ? "收起技術細節" : "技術細節"}
            <Icon name="chevronRight" strokeWidth={2} className={tech ? "is-rotated" : ""} />
          </button>
          {tech && <Tech route={route} calls={calls} />}
        </>
      )}
    </div>
  );
}

const STATE_TEXT: Record<StageState, string> = {
  ok: "通過",
  warn: "通過・有提醒",
  block: "擋下",
  skip: "沒有執行",
  doing: "執行中",
  todo: "等待中",
  handoff: "交給地端模組",
};

function Tech({ route: r, calls }: { route: RouteResponse; calls: JevCallInfo[] }) {
  return (
    <div className="tech">
      {r.question && (
        <section className="tech__section">
          <p className="tech__label">收到的文字（已遮蔽個資；之後各段與紀錄只看這個）</p>
          <pre className="code">{r.question}</pre>
          {r.pii.length > 0 && <p className="tech__note">已遮蔽：{r.pii.map((p) => `${p.kind} → ${p.code}`).join("、")}（原值不保留）</p>}
        </section>
      )}
      <section className="tech__section">
        <p className="tech__label">第 1 段・閘道驗過的 JWT payload（簽章不回傳；第 3 段的 Metadata Filter 只照它產生）</p>
        <pre className="code code--scroll">{JSON.stringify(r.auth.token?.claims ?? {}, null, 2)}</pre>
      </section>
      {r.router.engine === "local" && r.question && (
        <section className="tech__section">
          <p className="tech__label">
            第 2 段・本地分流（{r.router.model}・{r.router.latency_ms} ms）・信心 {r.confidence.toFixed(2)}／門檻 {r.threshold.toFixed(2)}
          </p>
          <div className="meter">
            <div className="meter__fill" style={{ width: `${Math.round(r.confidence * 100)}%` }} />
            <div className="meter__mark" style={{ left: `${r.threshold * 100}%` }} />
          </div>
          <dl className="kv">
            {r.ranked.map((x) => (
              <div key={x.intent} className="kv__row">
                <dt>{x.label}</dt>
                <dd>{x.prob.toFixed(2)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {calls.map((c) => (
        <JevSent key={c.stage} call={c} />
      ))}
    </div>
  );
}

function JevSent({ call }: { call: JevCallInfo }) {
  const [raw, setRaw] = useState(false);
  const codes = Object.entries(call.mapping);
  return (
    <section className="tech__section">
      <p className="tech__label">
        第 {call.stage} 段送給雲端 Jev（{SENT_LABEL[call.stage]}，代號化）・{kb(call.bytes)}・{call.latency_ms} ms
      </p>
      <pre className="code">{call.sent.join("\n")}</pre>
      {codes.length > 0 && <p className="tech__note">代號對照只留在本機：{codes.map(([code, e]) => `${code}＝${String((e as { label?: string }).label)}`).join("、")}</p>}
      <dl className="kv">
        {call.answers.map((a) => (
          <div key={a.label} className={`kv__row${a.alert ? " is-alert" : ""}`}>
            <dt>{a.label}</dt>
            <dd>{a.value}</dd>
          </div>
        ))}
      </dl>
      <button type="button" className="link-btn" onClick={() => setRaw((x) => !x)}>
        {raw ? "收起請求本文" : "看請求本文"}
        <Icon name="chevronRight" strokeWidth={2} className={raw ? "is-rotated" : ""} />
      </button>
      {raw && <pre className="code code--scroll">{`POST /v1/systemone\n${JSON.stringify(call.request, null, 2)}`}</pre>}
    </section>
  );
}
