/** 機密等級：「機密」圖紙不送往任何雲端 API（包含對照組） */
export function ConfidentialityBadge({ level }: { level: string }) {
  const style =
    level === "機密"
      ? "bg-danger text-white"
      : level === "內部"
        ? "bg-warning-soft text-warning"
        : "bg-success-soft text-success";
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold ${style}`}>
      <svg viewBox="0 0 16 16" className="h-3 w-3" fill="currentColor" aria-hidden>
        <path d="M8 1a3 3 0 0 0-3 3v2H4a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-1V4a3 3 0 0 0-3-3zm-1.5 5V4a1.5 1.5 0 0 1 3 0v2z" />
      </svg>
      {level}
    </span>
  );
}

/** 0–1 的分數條（IoU、線條重合度） */
export function ScoreMeter({ value, label, good = 0.7 }: { value: number; label?: string; good?: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const color = value >= good ? "bg-success" : value >= good / 2 ? "bg-warning" : "bg-danger";
  return (
    <div className="flex items-center gap-2">
      {label && <span className="w-14 shrink-0 text-xs text-ink-48">{label}</span>}
      <span className="h-2 flex-1 overflow-hidden rounded-full bg-parchment-deep">
        <span className={`block h-full rounded-full ${color}`} style={{ width: `${Math.max(pct, 3)}%` }} />
      </span>
      <span className="w-12 shrink-0 text-right font-mono text-sm font-semibold tabular-nums">{value.toFixed(3)}</span>
    </div>
  );
}
