import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { assetUrl, type ArtworkSummary } from "../../api/client";

export function ArtworkCard({
  artwork,
  to,
  badge,
  footer,
  dim = false,
}: {
  artwork: ArtworkSummary;
  to?: string;
  badge?: ReactNode;
  footer?: ReactNode;
  dim?: boolean;
}) {
  const body = (
    <div
      className={`card group flex h-full flex-col overflow-hidden transition hover:border-ink-48 ${dim ? "opacity-70" : ""}`}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-parchment-deep">
        <img
          src={assetUrl(artwork.thumb_url)}
          alt={artwork.title_zh}
          loading="lazy"
          className="h-full w-full object-cover object-[center_20%] transition duration-500 group-hover:scale-105"
        />
        {badge && <div className="absolute left-2 top-2">{badge}</div>}
      </div>
      <div className="flex flex-1 flex-col gap-1 p-4">
        <h3 className="text-lg font-semibold leading-snug text-ink">〈{artwork.title_zh}〉</h3>
        <p className="text-sm text-ink-80">
          {artwork.artist_zh} · {artwork.date_text}
        </p>
        <p className="text-xs text-ink-48">{artwork.collection}</p>
        {footer && <div className="mt-auto pt-2">{footer}</div>}
      </div>
    </div>
  );
  return to ? (
    <Link to={to} className="block h-full focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-focus">
      {body}
    </Link>
  ) : (
    body
  );
}
