import { useState } from "react";
import { api } from "../../api/client";

export function Loading({ label = "載入中…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-sm text-ink-soft" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-seal" />
      {label}
    </div>
  );
}

/** 錯誤訊息一律附 request_id，方便對照後端日誌（共用層 §七） */
export function ErrorMessage({
  title = "發生錯誤",
  message,
  requestId,
  code,
}: {
  title?: string;
  message: string;
  requestId?: string;
  code?: string;
}) {
  return (
    <div className="rounded-xl border border-seal/30 bg-seal-soft/60 p-3 text-sm">
      <p className="font-bold text-seal-deep">{title}</p>
      <p className="text-ink-soft">{message}</p>
      {(code || requestId) && (
        <p className="mt-1 font-mono text-[11px] text-ink-faint">
          {code} {requestId && `· request_id: ${requestId}`}
        </p>
      )}
    </div>
  );
}

export function FeedbackButtons({ requestId }: { requestId: string }) {
  const [sent, setSent] = useState<"up" | "down" | null>(null);
  const send = (rating: "up" | "down") => {
    setSent(rating);
    void api.feedback({ request_id: requestId, rating }).catch(() => setSent(null));
  };
  return (
    <span className="inline-flex items-center gap-1">
      {(["up", "down"] as const).map((r) => (
        <button
          key={r}
          type="button"
          disabled={!!sent}
          onClick={() => send(r)}
          className={`rounded-md px-1.5 py-0.5 text-sm transition ${
            sent === r ? "bg-ink text-white" : "text-ink-faint hover:bg-paper-deep disabled:opacity-40"
          }`}
          aria-label={r === "up" ? "回答有幫助" : "回答沒幫助"}
        >
          {r === "up" ? "👍" : "👎"}
        </button>
      ))}
    </span>
  );
}

export function LicenseLabel({
  license,
  attribution,
  sourceUrl,
}: {
  license: string;
  attribution?: string | null;
  sourceUrl?: string | null;
}) {
  return (
    <span className="text-xs text-ink-faint">
      授權：{license}
      {license === "CC BY 4.0" && attribution && <>（{attribution}）</>}
      {sourceUrl && (
        <>
          {" · "}
          <a href={sourceUrl} target="_blank" rel="noreferrer" className="underline hover:text-seal">
            出處
          </a>
        </>
      )}
    </span>
  );
}
