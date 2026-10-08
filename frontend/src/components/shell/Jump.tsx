import { MODULE, type Domain } from "../../shell/design";
import { Icon } from "./Icons";

/** 來源：ArtRAG-前端demo/source/src/components/Jump.tsx。簡介下方的跳轉按鈕：七段流程分派到哪個模組，按了直接進去 */
export function JumpCard({ domain, line, onGo, switching }: { domain: Domain; line: string; onGo: () => void; switching?: boolean }) {
  const m = MODULE[domain];
  return (
    <button type="button" className={`jump jump--${domain} fade-in`} onClick={onGo}>
      <span className="jump__icon">
        <Icon name={domain === "factory" ? "factory" : "palette"} strokeWidth={1.5} />
      </span>
      <span className="jump__body">
        <span className="jump__kicker">{switching ? `這一題屬於${m.label}` : `七段流程分派：${domain === "factory" ? "工廠" : "藝術"}相關・可以進階使用`}</span>
        <span className="jump__title">{switching ? `切換到${m.label}` : `進入${m.label}`}</span>
        <span className="jump__line">{line}</span>
        <span className="jump__asks">
          {m.asks.map((a) => (
            <span key={a}>{a}</span>
          ))}
        </span>
      </span>
      <span className="jump__go">
        <span>進入</span>
        <Icon name="arrowRight" strokeWidth={1.8} />
      </span>
    </button>
  );
}

/** 進入模組時的轉場：一層模組底色，帶出模組名稱 */
export function Warp({ domain }: { domain: Domain | null }) {
  if (!domain) return null;
  const m = MODULE[domain];
  return (
    <div className={`warp warp--${domain}`} aria-hidden>
      <span className="warp__name">{m.spaced}</span>
      <span className="warp__en">{m.en}</span>
    </div>
  );
}
