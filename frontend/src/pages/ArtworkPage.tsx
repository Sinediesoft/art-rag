import { Link, useParams } from "react-router-dom";
import { assetUrl, type ApiError } from "../api/client";
import { useArtwork } from "../api/hooks";
import { ArtworkColors } from "../components/color/ColorAnalysisCard";
import { ErrorMessage, LicenseLabel, Loading } from "../components/common/Feedback";

export function ArtworkPage() {
  const { id } = useParams();
  const { data: a, isLoading, error } = useArtwork(id);
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

  return (
    <article className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <div className="lg:sticky lg:top-20 lg:self-start">
        <div className="flex justify-center rounded-2xl bg-parchment-deep p-6 sm:p-10">
          <img
            src={assetUrl(a.image_url)}
            alt={a.title.zh}
            className="product-shadow max-h-[64vh] w-auto max-w-full object-contain"
          />
        </div>
        <div className="mt-3">
          <LicenseLabel license={a.image.license} attribution={a.image.attribution} sourceUrl={a.image.source_url} />
        </div>
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
          {a.descriptions.map((d, i) => (
            <div key={i} className="border-l-2 border-hairline pl-4">
              {d.topic && <h2 className="mb-1 text-lg font-semibold">{d.topic}</h2>}
              <p className="leading-relaxed text-ink-80">{d.text}</p>
              <LicenseLabel license={d.license} attribution={d.attribution} sourceUrl={d.source_url} />
            </div>
          ))}
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
