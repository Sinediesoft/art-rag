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
      className={`group flex h-full flex-col overflow-hidden rounded-xl border border-line bg-card shadow-sm transition hover:-translate-y-0.5 hover:shadow-md ${dim ? "opacity-70" : ""}`}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-paper-deep">
        <img
          src={assetUrl(artwork.thumb_url)}
          alt={artwork.title_zh}
          loading="lazy"
          className="h-full w-full object-cover object-[center_20%] transition duration-500 group-hover:scale-105"
        />
        {badge && <div className="absolute left-2 top-2">{badge}</div>}
      </div>
      <div className="flex flex-1 flex-col gap-1 p-3">
        <h3 className="font-serif text-lg font-bold leading-snug">〈{artwork.title_zh}〉</h3>
        <p className="text-sm text-ink-soft">
          {artwork.artist_zh} · {artwork.date_text}
        </p>
        <p className="text-xs text-ink-faint">{artwork.collection}</p>
        {footer && <div className="mt-auto pt-2">{footer}</div>}
      </div>
    </div>
  );
  return to ? (
    <Link to={to} className="block h-full focus:outline-none focus-visible:ring-2 focus-visible:ring-seal">
      {body}
    </Link>
  ) : (
    body
  );
}
