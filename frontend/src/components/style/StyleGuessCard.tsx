import type { ApiError, StyleField, StyleGuess } from "../../api/client";
import { usePhotoStyle } from "../../api/hooks";
import { ErrorMessage, Loading } from "../common/Feedback";

const pct = (v: number) => `${Math.round(v * 100)}%`;

/** 機率條：數值直接標在旁邊（不靠 tooltip） */
function ProbBar({ value, muted }: { value: number; muted?: boolean }) {
  return (
    <span className="flex items-center gap-2 text-[11px] text-ink-48">
      <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-parchment-deep" aria-hidden>
        <span
          className={`block h-full rounded-full ${muted ? "bg-ink-48" : "bg-accent"}`}
          style={{ width: `${Math.max(3, Math.min(100, value * 100))}%` }}
        />
      </span>
      <span className="w-8 shrink-0 text-right tabular-nums">{pct(value)}</span>
    </span>
  );
}

function FieldTile({ f }: { f: StyleField }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-hairline p-3">
      <dt className="text-xs font-semibold text-ink-48">{f.label}</dt>
      <dd className={`text-base font-semibold ${f.uncertain ? "text-ink-48" : "text-ink"}`}>
        {f.uncertain ? "看不太出來" : f.name}
        {f.period && !f.uncertain && <span className="block text-xs font-normal text-ink-80">{f.period}</span>}
      </dd>
      <dd>
        <ProbBar value={f.prob} muted={f.uncertain} />
      </dd>
      <dd className="mt-1 border-t border-hairline pt-1.5">
        <p className="mb-1 text-[11px] text-ink-48">{f.key === "style" ? "細分流派（僅供參考）" : "最接近的幾個"}</p>
        <ul className="flex flex-col gap-0.5">
          {f.candidates.map((c) => (
            <li key={c.name} className="grid grid-cols-[minmax(0,5.5rem)_1fr] items-center gap-2 text-xs text-ink-80">
              <span className="truncate" title={c.group ? `${c.name}（${c.group}）` : c.name}>
                {c.name}
              </span>
              <ProbBar value={c.prob} muted />
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

/** 畫作卡推測（docs/adr/018，借鑒 ArtSeek 的畫作卡）：知識庫沒有這幅畫時，推測風格大類、題材、媒材 */
export function StyleGuessCard({ data }: { data: StyleGuess }) {
  return (
    <section className="card flex flex-col gap-4 p-5">
      <header>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-semibold">畫作卡（推測）</h2>
          <span className="inline-flex items-center gap-1 rounded-full bg-warning-soft px-2 py-0.5 text-xs font-semibold text-warning">
            <span className="h-1.5 w-1.5 rounded-full bg-warning" />
            推測・沒有出處
          </span>
        </div>
        <p className="text-sm text-ink-80">{data.summary}</p>
      </header>

      {data.is_painting && (
        <dl className="grid gap-3 sm:grid-cols-3">
          {data.fields.map((f) => (
            <FieldTile key={f.key} f={f} />
          ))}
        </dl>
      )}

      <footer className="flex flex-col gap-1 text-xs text-ink-48">
        {data.is_painting && (
          <ul className="list-disc pl-4">
            {data.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        )}
        <p>
          本機 Chinese-CLIP 零樣本比對（和辨識同一個模型，不用訓練、照片不出站）· 像畫作的程度{" "}
          {pct(data.painting_score)} · {data.latency_ms} ms
        </p>
      </footer>
    </section>
  );
}

export function PhotoStyleGuess({ imageId }: { imageId: string }) {
  const { data, isLoading, error } = usePhotoStyle(imageId);
  if (isLoading) return <Loading label="推測風格、題材與媒材…" />;
  if (error || !data)
    return (
      <ErrorMessage
        title="畫作卡推測失敗"
        message={(error as Error)?.message ?? ""}
        requestId={(error as ApiError)?.requestId}
      />
    );
  return <StyleGuessCard key={imageId} data={data} />;
}
