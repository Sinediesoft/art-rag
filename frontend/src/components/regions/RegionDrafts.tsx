// 區域解說的草稿（docs/adr/030 第 2 步）：主管在核准頁收錄或退回；藝術家看自己送的進度、撤回。
// 收錄只能在這裡按按鈕，不接受用對話收錄（和超額申請的核准一樣）。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type RegionDraft } from "../../api/client";
import { useRegionDrafts } from "../../api/hooks";
import { formatTaipei } from "../../lib/format";
import { RegionThumb } from "./RegionOverlay";

const STATUS: Record<RegionDraft["status"], [string, string]> = {
  pending: ["待收錄", "bg-warning-soft text-warning"],
  indexing: ["收錄中（重建索引）", "bg-accent-soft text-accent"],
  done: ["已收錄", "bg-success-soft text-success"],
  failed: ["收錄失敗（已還原）", "bg-danger-soft text-danger"],
  returned: ["已退回", "bg-parchment-deep text-ink-48"],
};

export function RegionDraftsSection() {
  const { data } = useRegionDrafts();
  if (!data || (!data.can_commit && data.mine.length === 0)) return null;
  return (
    <section className="flex flex-col gap-3">
      {data.can_commit && (
        <>
          <h2 className="text-lg font-semibold">藝術家的區域解說・待收錄（{data.pending.length}）</h2>
          <p className="text-sm text-ink-80">
            收錄後寫進知識庫（畫作 JSON 加一塊區域和一段解說）、重建索引，畫作頁與問答就會用到；重建失敗會自動還原。
          </p>
          {data.pending.length === 0 && <p className="text-sm text-ink-48">目前沒有待收錄的區域解說。</p>}
          {data.pending.map((d) => (
            <DraftCard key={d.draft_id} d={d} canCommit />
          ))}
        </>
      )}
      {data.mine.length > 0 && (
        <>
          <h2 className="text-lg font-semibold">我送出的區域解說</h2>
          {data.mine.map((d) => (
            <DraftCard key={d.draft_id} d={d} mine />
          ))}
        </>
      )}
    </section>
  );
}

function DraftCard({ d, canCommit = false, mine = false }: { d: RegionDraft; canCommit?: boolean; mine?: boolean }) {
  const qc = useQueryClient();
  const [returning, setReturning] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 背景重建索引完成：畫作頁要重抓才看得到新的區域
  useEffect(() => {
    if (d.status === "done") void qc.invalidateQueries({ queryKey: ["artwork", d.artwork_id] });
  }, [d.status, d.artwork_id, qc]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await qc.invalidateQueries({ queryKey: ["region-drafts"] });
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const [label, cls] = STATUS[d.status];
  const open = d.status === "pending" || d.status === "failed";
  return (
    <article className="card flex flex-col gap-4 p-5 sm:flex-row">
      <div className="shrink-0 text-xs">
        <RegionThumb artworkId={d.artwork_id} region={{ id: d.draft_id, label: d.label, points: d.points }} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2 text-sm">
        <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={`rounded-full px-2 py-0.5 text-xs ${cls}`}>{label}</span>
          <Link to={`/artworks/${d.artwork_id}`} className="link">
            〈{d.artwork_title}〉
          </Link>
          <span className="font-semibold">{d.label}</span>
          <span className="text-xs text-ink-48">
            {d.by_label} · {formatTaipei(d.created_at)}
          </span>
        </header>
        <p className="whitespace-pre-wrap leading-relaxed text-ink-80">{d.text}</p>
        <p className="text-xs text-ink-48">
          授權 {d.license}
          {d.attribution ? `・署名「${d.attribution}」` : ""}
        </p>
        {d.status === "returned" && d.review?.reason && (
          <p className="text-xs text-danger">
            退回原因：{d.review.reason}（{d.review.by_label}）
          </p>
        )}
        {d.status === "failed" && d.commit?.error && <p className="text-xs text-danger">{d.commit.error}</p>}
        {d.status === "done" && d.commit && (
          <p className="text-xs text-success">
            已寫進知識庫（版本 {d.commit.kb_version}，{d.commit.by_label}收錄），畫作頁看得到這一塊。
          </p>
        )}
        {error && <p className="rounded-lg bg-danger-soft px-3 py-1.5 text-xs text-danger">{error}</p>}

        {canCommit && open && !returning && (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-primary px-4 py-1.5 text-sm"
              disabled={busy}
              onClick={() => run(() => api.commitRegionDraft(d.draft_id))}
            >
              {d.status === "failed" ? "再收錄一次" : "收錄進知識庫"}
            </button>
            <button type="button" className="btn-ghost px-4 py-1.5 text-sm" disabled={busy} onClick={() => setReturning(true)}>
              退回
            </button>
          </div>
        )}
        {canCommit && open && returning && (
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={reason}
              maxLength={200}
              onChange={(e) => setReason(e.target.value)}
              placeholder="退回原因（藝術家看得到）"
              aria-label="退回原因"
              className="min-w-0 flex-1 rounded-lg border border-hairline bg-white px-2 py-1.5 text-sm outline-none focus:border-accent"
            />
            <button
              type="button"
              className="btn-primary px-4 py-1.5 text-sm"
              disabled={busy || !reason.trim()}
              onClick={() => run(() => api.returnRegionDraft(d.draft_id, reason.trim()))}
            >
              確定退回
            </button>
            <button type="button" className="link text-sm" onClick={() => setReturning(false)}>
              取消
            </button>
          </div>
        )}
        {mine && d.status !== "done" && d.status !== "indexing" && (
          <button
            type="button"
            className="link self-start text-xs"
            disabled={busy}
            onClick={() => run(() => api.withdrawRegionDraft(d.draft_id))}
          >
            撤回這份草稿
          </button>
        )}
      </div>
    </article>
  );
}
