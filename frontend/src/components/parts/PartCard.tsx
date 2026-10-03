import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { assetUrl, type PartSummary } from "../../api/client";
import { ConfidentialityBadge } from "./PartBadges";

export function PartCard({
  part,
  to,
  badge,
  footer,
  dim = false,
}: {
  part: PartSummary;
  to?: string;
  badge?: ReactNode;
  footer?: ReactNode;
  dim?: boolean;
}) {
  const g = part.geometry;
  const body = (
    <div
      className={`card group flex h-full flex-col overflow-hidden transition hover:border-ink-48 ${dim ? "opacity-70" : ""}`}
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-canvas">
        <img
          src={assetUrl(part.thumb_url)}
          alt={part.name_zh}
          loading="lazy"
          className="h-full w-full object-contain p-1 transition duration-500 group-hover:scale-105"
        />
        <div className="absolute left-2 top-2 flex gap-1">
          {badge}
          <ConfidentialityBadge level={part.confidentiality} />
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-1 p-3">
        <p className="font-mono text-[11px] text-ink-48">
          {part.part_no} · {part.drawing_no} rev.{part.revision}
        </p>
        <h3 className="text-lg font-semibold leading-snug">{part.name_zh}</h3>
        <p className="text-sm text-ink-80">{part.material}</p>
        <p className="text-xs text-ink-48">
          {g.width}×{g.depth}×{g.height} mm · {g.weight_kg.toFixed(3)} kg
        </p>
        {footer && <div className="mt-auto pt-2">{footer}</div>}
      </div>
    </div>
  );
  return to ? (
    <Link to={to} className="block h-full rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-focus">
      {body}
    </Link>
  ) : (
    body
  );
}
