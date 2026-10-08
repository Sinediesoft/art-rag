import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useAccounts } from "../api/hooks";
import { streamBatch, type BatchDone, type BatchRow, type BatchStatus } from "../api/sse";
import { seconds } from "../lib/format";
import { preprocessImage } from "../lib/image";
import { download, safeName, stamp, toCsv } from "../lib/export";

const MAX = 100; // shared/models.yaml 的 batch.max_images

type Domain = "auto" | "art" | "mfg";
const DOMAINS: { value: Domain; label: string; hint: string }[] = [
  { value: "auto", label: "自動判斷", hint: "每張照片先判斷是畫作還是工廠圖紙" },
  { value: "art", label: "只找畫作", hint: "典藏盤點：整批都是畫作" },
  { value: "mfg", label: "只找圖紙", hint: "舊圖紙歸檔：整批都是工廠圖紙" },
];

const STATUS: Record<BatchStatus, { label: string; cls: string }> = {
  matched: { label: "認得", cls: "bg-success-soft text-success" },
  not_in_kb: { label: "不在知識庫", cls: "bg-warning-soft text-warning" },
  blurry: { label: "太模糊", cls: "bg-danger-soft text-danger" },
  hidden: { label: "看不到", cls: "bg-parchment-deep text-ink-80" },
  error: { label: "失敗", cls: "bg-danger-soft text-danger" },
};

interface Photo {
  name: string;
  imageId: string | null;
  error?: string;
}

/**
 * 批次辨識（docs/adr/017）：一次選一批照片，逐張辨識（和以圖搜圖同一套），結果一列一列出現，可以匯出 CSV。
 * 畫作：典藏盤點，哪幾幅已建檔、哪幾幅沒有；工廠：舊圖紙歸檔，一疊照片對回料號與版次。
 * 「不在知識庫」那幾列可以直接拍照建檔（docs/adr/013）。
 */
export function BatchPage() {
  const me = useAccounts().data?.current;
  const canMfg = me?.domains.includes("mfg") ?? false;
  const [domain, setDomain] = useState<Domain>("auto");
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [rows, setRows] = useState<Record<number, BatchRow>>({});
  const [done, setDone] = useState<BatchDone | null>(null);
  const [phase, setPhase] = useState<"idle" | "uploading" | "running" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<BatchStatus | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);
  // 卸載（離開頁面、切換身分時功能頁重建）就中止：舊身分的結果不再接收
  useEffect(() => () => abort.current?.abort(), []);

  const start = async (files: File[]) => {
    const images = files.filter((f) => f.type.startsWith("image/"));
    if (!images.length) return;
    if (images.length > MAX) {
      setError(`一批最多 ${MAX} 張（選了 ${images.length} 張），請分批`);
      return;
    }
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setError(null);
    setRows({});
    setDone(null);
    setFilter(null);
    setPhase("uploading");
    // 逐張在瀏覽器轉正、縮圖、去 EXIF 再上傳（和單張上傳同一套前處理）
    const list: Photo[] = images.map((f) => ({ name: f.name, imageId: null }));
    setPhotos([...list]);
    for (let i = 0; i < images.length; i++) {
      if (ctrl.signal.aborted) return;
      try {
        const { image_id } = await api.uploadImage(await preprocessImage(images[i]));
        list[i] = { ...list[i], imageId: image_id };
      } catch (e) {
        list[i] = { ...list[i], error: e instanceof ApiError ? e.message : (e as Error).message };
      }
      setPhotos([...list]);
    }
    const ok = list.map((p, i) => [p, i] as const).filter(([p]) => p.imageId);
    if (!ok.length) {
      setPhase("idle");
      setError("沒有任何一張上傳成功");
      return;
    }
    setPhase("running");
    // 後端的 index 是送出的順序：對回原本的照片
    const order = ok.map(([, i]) => i);
    await streamBatch(
      ok.map(([p]) => p.imageId!),
      domain === "auto" ? null : domain,
      {
        onRow: (r) => setRows((prev) => ({ ...prev, [order[r.index]]: { ...r, index: order[r.index] } })),
        onDone: (d) => {
          setDone(d);
          setPhase("done");
        },
        onError: (e) => {
          setError(`${e.message}（${e.code}）`);
          setPhase("done");
        },
      },
      ctrl.signal,
    );
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (phase === "uploading" || phase === "running") return;
    void start(Array.from(e.dataTransfer.files));
  };

  const finished = Object.values(rows);
  const exportCsv = async () => {
    const header = [
      "序號", "檔名", "領域", "結果", "ID", "名稱", "說明", "機密等級",
      "CLIP 相似度", "幾何驗證對應點", "線條重合", "模糊程度", "最相近（不在知識庫時）", "備註", "照片 ID",
    ];
    const lines = photos.map((p, i) => {
      const r = rows[i];
      const item = r?.item;
      return [
        i + 1,
        p.name,
        r?.domain === "mfg" ? "工廠圖紙" : r?.domain === "art" ? "畫作" : "",
        r ? STATUS[r.status].label : p.error ? `上傳失敗：${p.error}` : "",
        item?.id ?? "",
        item?.label ?? "",
        item?.detail ?? "",
        item?.level ?? "",
        r?.score ?? "",
        r?.inliers ?? "",
        r?.overlap ?? "",
        r?.blur ?? "",
        r?.closest ? `${r.closest.id} ${r.closest.label}` : "",
        r?.note ?? "",
        p.imageId ?? "",
      ];
    });
    const refs = [...new Set(finished.flatMap((r) => [r.item?.id, r.closest?.id]).filter((x): x is string => !!x))];
    // 先記稽核再下載：記不到（例如憑證過期）就不匯出
    try {
      await api.logExport({ kind: "batch_csv", refs: refs.slice(0, 200), rows: lines.length, title: "批次辨識" });
    } catch (e) {
      setError(`沒有匯出：${e instanceof ApiError ? e.message : String(e)}`);
      return;
    }
    download(`批次辨識-${safeName(me?.label ?? "")}-${stamp()}.csv`, toCsv([header, ...lines]), "text/csv;charset=utf-8");
  };

  const busy = phase === "uploading" || phase === "running";
  const uploaded = photos.filter((p) => p.imageId || p.error).length;
  const shown = photos.map((p, i) => [p, rows[i], i] as const).filter(([, r]) => !filter || r?.status === filter);
  const counts = finished.reduce<Partial<Record<BatchStatus, number>>>(
    (c, r) => ({ ...c, [r.status]: (c[r.status] ?? 0) + 1 }),
    {},
  );

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="t-display">批次辨識</h1>
        <p className="max-w-3xl text-sm text-ink-80">
          一次選一批照片，每張都用以圖搜圖同一套方法辨識：畫作做<b className="text-ink">典藏盤點</b>
          （哪幾幅已建檔、哪幾幅還沒有），工廠圖紙做<b className="text-ink">舊圖紙歸檔</b>（對回料號與版次）。
          太模糊的照片會標出來請你重拍，不硬判；目前身分看不到的圖紙只標「看不到」。結果可以匯出 CSV，全程在本機處理。
        </p>
      </header>

      <section className="card flex flex-col gap-4 p-5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="t-fine font-semibold text-ink-48">照片是</span>
          <div className="flex flex-wrap rounded-full border border-hairline bg-card p-1">
            {DOMAINS.map((d) => {
              const disabled = busy || (d.value === "mfg" && !canMfg);
              return (
                <button
                  key={d.value}
                  type="button"
                  title={d.value === "mfg" && !canMfg ? "目前身分不能使用工廠圖紙" : d.hint}
                  disabled={disabled}
                  onClick={() => setDomain(d.value)}
                  className={`rounded-full px-3 py-1 text-sm font-normal transition disabled:opacity-40 ${
                    domain === d.value ? "bg-accent text-white" : "text-ink-80 hover:bg-parchment-deep"
                  }`}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`flex flex-col items-center gap-3 rounded-xl border-2 border-dashed p-6 text-center transition ${
            dragging ? "border-accent bg-accent-soft" : "border-hairline"
          }`}
        >
          <button type="button" disabled={busy} onClick={() => fileRef.current?.click()} className="btn-primary">
            {phase === "uploading"
              ? `上傳中 ${uploaded}/${photos.length}…`
              : phase === "running"
                ? `辨識中 ${finished.length}/${photos.filter((p) => p.imageId).length}…`
                : photos.length
                  ? "再選一批照片"
                  : "選照片（可以一次選很多張）"}
          </button>
          <p className="text-xs text-ink-48">或把照片拖進這個框・一批最多 {MAX} 張・每張約 0.5–1 秒</p>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              void start(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
        </div>
        {error && <p className="rounded-lg bg-danger-soft p-2 text-sm text-danger">{error}</p>}
      </section>

      {photos.length > 0 && (
        <section className="card flex flex-col gap-3 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5 text-sm">
              <button
                type="button"
                onClick={() => setFilter(null)}
                className={`chip ${filter === null ? "border-accent text-accent" : ""}`}
              >
                全部 {photos.length}
              </button>
              {(Object.keys(STATUS) as BatchStatus[])
                .filter((k) => counts[k])
                .map((k) => (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setFilter(filter === k ? null : k)}
                    className={`chip ${filter === k ? "border-accent text-accent" : ""}`}
                  >
                    {STATUS[k].label} {counts[k]}
                  </button>
                ))}
            </div>
            <button type="button" disabled={phase !== "done" || !finished.length} onClick={() => void exportCsv()} className="btn-ghost px-4 py-1.5 text-sm">
              匯出 CSV
            </button>
          </div>
          {done && (
            <p className="text-xs text-ink-48">
              {done.total} 張・畫作 {done.domains.art}、圖紙 {done.domains.mfg}・共 {seconds(done.latency_ms.total)}（每張約{" "}
              {seconds(done.latency_ms.per_image)}）・只用 Chinese-CLIP 與幾何驗證，外送 {done.egress.bytes} bytes
            </p>
          )}

          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-ink-48">
                  <th className="w-8 py-1.5 font-normal">#</th>
                  <th className="py-1.5 font-normal">照片</th>
                  <th className="py-1.5 font-normal">結果</th>
                  <th className="py-1.5 font-normal">比對到</th>
                  <th className="py-1.5 font-normal">依據</th>
                  <th className="py-1.5 font-normal" />
                </tr>
              </thead>
              <tbody>
                {shown.map(([p, r, i]) => (
                  <BatchLine key={i} index={i} photo={p} row={r} canMfg={canMfg} />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function BatchLine({ index, photo, row, canMfg }: { index: number; photo: Photo; row?: BatchRow; canMfg: boolean }) {
  const st = row ? STATUS[row.status] : null;
  const intake =
    row?.status === "not_in_kb" && photo.imageId
      ? row.domain === "art"
        ? { to: `/artworks/intake?image=${photo.imageId}`, label: "拍照建檔：這是一幅畫" }
        : row.domain === "mfg" && canMfg
          ? { to: `/drawings/intake?image=${photo.imageId}`, label: "拍照建檔：這是一張圖紙" }
          : null
      : null;
  return (
    <tr className="border-b border-hairline/60 align-top">
      <td className="py-2 font-mono text-xs text-ink-48">{index + 1}</td>
      <td className="py-2">
        <div className="flex items-center gap-2">
          {photo.imageId ? (
            <img src={api.uploadedImageUrl(photo.imageId)} alt="" className="h-11 w-11 shrink-0 rounded-md object-cover ring-1 ring-hairline" />
          ) : (
            <span className="grid h-11 w-11 shrink-0 place-items-center rounded-md bg-parchment-deep text-[10px] text-ink-48">
              {photo.error ? "失敗" : "…"}
            </span>
          )}
          <span className="max-w-[12rem] truncate text-xs text-ink-80" title={photo.name}>
            {photo.name}
          </span>
        </div>
      </td>
      <td className="py-2">
        {st ? (
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${st.cls}`}>{st.label}</span>
        ) : photo.error ? (
          <span className="text-xs text-danger">上傳失敗：{photo.error}</span>
        ) : (
          <span className="text-xs text-ink-48">等待中</span>
        )}
        {row?.domain && (
          <span className="ml-1.5 text-xs text-ink-48">{row.domain === "art" ? "畫作" : "圖紙"}</span>
        )}
      </td>
      <td className="py-2">
        {row?.item ? (
          <Link to={row.item.url} className="link">
            〈{row.item.label}〉
            <span className="block text-xs text-ink-48">
              {row.item.detail}
              {row.item.level !== "公開" && ` · ${row.item.level}`}
            </span>
          </Link>
        ) : row?.closest ? (
          <span className="text-xs text-ink-48">
            最相近：〈{row.closest.label}〉（{row.closest.id}），沒有通過幾何驗證
          </span>
        ) : (
          row?.note && <span className="text-xs text-ink-48">{row.note}</span>
        )}
      </td>
      <td className="py-2 font-mono text-xs text-ink-48">
        {row?.score != null && <div>相似度 {row.score.toFixed(2)}</div>}
        {row?.inliers != null && <div>對應點 {row.inliers}</div>}
        {row?.overlap != null && <div>線條重合 {row.overlap.toFixed(2)}</div>}
        {row?.blur != null && <div>模糊 {row.blur.toFixed(2)}</div>}
      </td>
      <td className="py-2 text-right text-xs">
        {intake && (
          <Link to={intake.to} className="link whitespace-nowrap">
            {intake.label} →
          </Link>
        )}
      </td>
    </tr>
  );
}
