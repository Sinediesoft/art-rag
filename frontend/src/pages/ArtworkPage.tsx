import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { assetUrl, type ApiError } from "../api/client";
import { useAccounts, useArtwork } from "../api/hooks";
import { ArtworkColors } from "../components/color/ColorAnalysisCard";
import { ErrorMessage, LicenseLabel, Loading } from "../components/common/Feedback";
import { RegionEditor } from "../components/regions/RegionEditor";
import { RegionOverlay, regionWhere } from "../components/regions/RegionOverlay";

export function ArtworkPage() {
  const { id } = useParams();
  const { data: a, isLoading, error } = useArtwork(id);
  // 標亮的區域（docs/adr/029）：點畫上的區域 → 捲到講它的段落；點段落的「在畫上標出」→ 畫上標亮
  const [active, setActive] = useState<string | null>(null);
  const imageRef = useRef<HTMLDivElement>(null);
  // 藝術家在分給自己的畫上圈區域、寫解說（docs/adr/029 第 2 步）
  const me = useAccounts().data?.current;
  const [editing, setEditing] = useState(false);
  useEffect(() => setEditing(false), [id, me?.id]);
  if (isLoading) return <Loading />;
  if (error || !a)
    return (
      <ErrorMessage
        title="找不到畫作"
        message={(error as Error)?.message ?? ""}
        requestId={(error as ApiError)?.requestId}
      />
    );

  const meta: [string, string | null | undefined][] = [
    ["作者", `${a.artist.zh}${a.artist.en ? `（${a.artist.en}）` : ""}`],
    ["年代", a.date_text],
    ["材質", a.medium],
    ["尺寸", a.dimensions],
    ["館藏", a.collection],
    ["館藏編號", a.source_id],
  ];

  const regions = a.regions?.items ?? [];
  const canAnnotate = !!me && me.ops.includes("kb_annotate") && me.artworks.includes(a.id);
  // 同一塊區域有好幾段時，捲到第一段
  const firstPassage = new Map<string, number>();
  a.descriptions.forEach((d, i) => {
    if (d.region && !firstPassage.has(d.region)) firstPassage.set(d.region, i);
  });
  const select = (rid: string, from: "image" | "text") => {
    const next = active === rid ? null : rid;
    setActive(next);
    if (!next) return;
    // 窄螢幕的圖在段落上方、不會跟著捲：從段落點的時候把圖捲回來
    const target = from === "image" ? document.getElementById(`region-${rid}`) : imageRef.current;
    target?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  return (
    <article className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <div className={editing ? "" : "lg:sticky lg:top-20 lg:self-start"}>
        {editing && me ? (
          <RegionEditor artwork={a} me={me} onClose={() => setEditing(false)} />
        ) : (
          <>
            <div ref={imageRef} className="flex justify-center rounded-2xl bg-parchment-deep p-6 sm:p-10">
              <div className="relative w-fit max-w-full">
                <img
                  src={assetUrl(a.image_url)}
                  alt={a.title.zh}
                  className="product-shadow block max-h-[64vh] w-auto max-w-full"
                />
                {regions.length > 0 && (
                  <RegionOverlay regions={regions} active={active} onSelect={(rid) => select(rid, "image")} />
                )}
              </div>
            </div>
            <div className="mt-3">
              <LicenseLabel license={a.image.license} attribution={a.image.attribution} sourceUrl={a.image.source_url} />
            </div>
            {regions.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                <span className="text-ink-48">畫上圈出的區域：</span>
                {regions.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    aria-pressed={active === r.id}
                    onClick={() => select(r.id, "image")}
                    className={`rounded-full px-2.5 py-0.5 transition ${
                      active === r.id ? "bg-accent text-white" : "bg-parchment-deep text-ink-80 hover:text-accent"
                    }`}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            )}
            {canAnnotate && (
              <button
                type="button"
                className="btn-ghost mt-3 px-4 py-1.5 text-sm"
                onClick={() => {
                  setActive(null);
                  setEditing(true);
                }}
              >
                在畫上圈一塊、寫解說
              </button>
            )}
          </>
        )}
      </div>

      <div className="flex flex-col gap-5">
        <header>
          <p className="t-eyebrow">{a.title.en}</p>
          <h1 className="t-display mt-1">〈{a.title.zh}〉</h1>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {a.style_tags.map((t) => (
              <span key={t} className="rounded-full bg-parchment-deep px-2.5 py-0.5 text-xs text-ink-80">
                {t}
              </span>
            ))}
          </div>
        </header>

        <Link
          to={`/artworks/${a.id}/chat`}
          className="btn-primary w-full justify-between"
        >
          <span>問問這幅畫</span>
          <span aria-hidden>→</span>
        </Link>

        <dl className="card grid grid-cols-[5em_1fr] gap-x-3 gap-y-1.5 p-5 text-sm">
          {meta
            .filter(([, v]) => v)
            .map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-ink-48">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
        </dl>

        <ArtworkColors artworkId={a.id} />

        <section className="flex flex-col gap-4">
          {a.descriptions.map((d, i) => {
            const r = d.region ? regions.find((x) => x.id === d.region) : undefined;
            const on = !!r && active === r.id;
            return (
              <div
                key={i}
                id={r && firstPassage.get(r.id) === i ? `region-${r.id}` : undefined}
                className={`border-l-2 pl-4 transition ${on ? "rounded-r-md border-accent bg-accent-soft py-2" : "border-hairline"}`}
              >
                {d.topic && <h2 className="mb-1 text-lg font-semibold">{d.topic}</h2>}
                {r && (
                  <button
                    type="button"
                    aria-pressed={on}
                    onClick={() => select(r.id, "text")}
                    className="mb-1 text-xs text-accent hover:underline"
                  >
                    畫面{regionWhere(r.points)}・{r.label}（{on ? "取消標示" : "在畫上標出"}）
                  </button>
                )}
                <p className="leading-relaxed text-ink-80">{d.text}</p>
                <LicenseLabel license={d.license} attribution={d.attribution} sourceUrl={d.source_url} />
              </div>
            );
          })}
        </section>

        <p className="text-xs text-ink-48">
          畫作 ID <code className="font-mono">{a.id}</code> · 資料來源{" "}
          <a href={a.source_url} target="_blank" rel="noreferrer" className="link">
            {new URL(a.source_url).hostname}
          </a>
        </p>
      </div>
    </article>
  );
}
