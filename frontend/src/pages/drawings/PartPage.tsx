import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { assetUrl, type ApiError } from "../../api/client";
import { usePart, usePartReconstructions } from "../../api/hooks";
import { ApiErrorMessage, Loading } from "../../components/common/Feedback";
import { PartInventoryCard } from "../../components/inventory/PartInventoryCard";
import { ModelViewer } from "../../components/LazyModelViewer";
import { PartProductionCard } from "../../components/schedule/PartProductionCard";
import { ConfidentialityBadge } from "../../components/parts/PartBadges";
import { formatTaipei, seconds } from "../../lib/format";

export function PartPage() {
  const { id } = useParams();
  const { data: p, isLoading, error } = usePart(id);
  const history = usePartReconstructions(id);
  const [view, setView] = useState<"drawing" | "model">("drawing");
  if (isLoading) return <Loading />;
  if (error || !p)
    return (
      <ApiErrorMessage
        error={error}
        title={(error as ApiError)?.code === "DATA_SCOPE_DENIED" ? undefined : "找不到圖紙"}
      />
    );

  const g = p.geometry;
  const meta: [string, string | null | undefined][] = [
    ["料號", p.part_no],
    ["圖號", `${p.drawing_no}（版次 ${p.revision}）`],
    ["類別", p.category],
    ["材料", `${p.material}（密度 ${p.density_g_cm3} g/cm³）`],
    ["表面處理", p.surface],
    ["外形尺寸", `${g.width} × ${g.depth} × ${g.height} mm（寬×深×高）`],
    ["估算重量", `${g.weight_kg.toFixed(3)} kg（體積 ${Math.round(g.volume_mm3).toLocaleString()} mm³）`],
    ["負責單位", `${p.owner} · ${p.company}`],
  ];
  const done = (history.data?.items ?? []).filter((r) => r.ok);

  return (
    <article className="grid gap-6 lg:grid-cols-[minmax(0,6fr)_minmax(0,5fr)]">
      <div className="flex flex-col gap-2 lg:sticky lg:top-20 lg:self-start">
        <div className="flex rounded-xl border border-line bg-card p-1 text-sm font-medium">
          {(
            [
              ["drawing", "加工圖"],
              ["model", "標準 3D 模型"],
            ] as const
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              onClick={() => setView(k)}
              className={`flex-1 rounded-lg px-3 py-1.5 transition ${
                view === k ? "bg-steel text-white" : "text-ink-soft hover:bg-paper-deep"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {view === "drawing" ? (
          <div className="overflow-hidden rounded-2xl border border-line bg-white shadow-sm">
            <img src={assetUrl(p.drawing_url)} alt={p.name.zh} className="w-full object-contain" />
          </div>
        ) : (
          <ModelViewer layers={[{ url: p.model_url, color: "#9fb6cc" }]} height={420} />
        )}
        <p className="text-xs text-ink-faint">
          {view === "drawing"
            ? "第一角法三視圖，由標準 CadQuery 模型自動產生（格式與 Ortho2CAD 訓練資料相同）"
            : "標準 3D 模型（kb/cad 的 CadQuery 程式碼）：用來和 Ortho2CAD 的重建結果比 IoU"}
          {" · "}
          <a href={assetUrl(p.step_url)} className="text-steel underline">
            下載 STEP
          </a>
        </p>
      </div>

      <div className="flex flex-col gap-5">
        <header>
          <p className="font-mono text-sm text-steel">
            {p.part_no} · {p.name.en}
          </p>
          <h1 className="flex flex-wrap items-center gap-2 text-3xl font-black">
            {p.name.zh} <ConfidentialityBadge level={p.confidentiality} />
          </h1>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {p.tags.map((t) => (
              <span key={t} className="rounded-full bg-steel-soft px-2.5 py-0.5 text-xs text-steel-deep">
                {t}
              </span>
            ))}
          </div>
        </header>

        <div className="grid gap-2 sm:grid-cols-2">
          <Link
            to={`/drawings/${p.id}/reconstruct`}
            className="flex items-center justify-between rounded-xl bg-steel px-4 py-3 font-bold text-white shadow-sm transition hover:bg-steel-deep"
          >
            <span>Ortho2CAD 3D 重建</span>
            <span aria-hidden>→</span>
          </Link>
          <Link
            to={`/drawings/${p.id}/chat`}
            className="flex items-center justify-between rounded-xl bg-ink px-4 py-3 font-bold text-paper shadow-sm transition hover:bg-ink/85"
          >
            <span>問問這張圖</span>
            <span aria-hidden>→</span>
          </Link>
        </div>

        {done.length > 0 && (
          <div className="rounded-xl border border-line bg-card p-3 text-sm">
            <p className="mb-1 text-xs font-bold text-ink-faint">最近的 3D 重建（不必重跑，直接看結果）</p>
            <ul className="flex flex-col gap-1">
              {done.slice(0, 3).map((r) => (
                <li key={r.job_id}>
                  <Link
                    to={`/drawings/${p.id}/reconstruct?job=${r.job_id}`}
                    className="flex flex-wrap items-center gap-x-3 rounded-md px-2 py-1 hover:bg-paper-deep"
                  >
                    <span className="font-mono text-xs text-ink-faint">{formatTaipei(r.created_at)}</span>
                    <span className="font-medium">{r.strategy === "ortho2cad" ? "Ortho2CAD" : "Qwen3-VL 對照組"}</span>
                    <span>IoU {r.iou != null ? r.iou.toFixed(3) : "—"}</span>
                    {r.iou_bbox != null && <span className="text-ink-faint">外框對齊 {r.iou_bbox.toFixed(3)}</span>}
                    <span className="text-ink-faint">{seconds(r.total_ms)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}

        <PartInventoryCard partId={p.id} name={p.name.zh} />

        <PartProductionCard partId={p.id} />

        <dl className="grid grid-cols-[5.5em_1fr] gap-x-3 gap-y-1.5 rounded-xl border border-line bg-card p-4 text-sm">
          {meta
            .filter(([, v]) => v)
            .map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-ink-faint">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
        </dl>

        <section className="flex flex-col gap-4">
          {p.descriptions.map((d, i) => (
            <div key={i} className="border-l-2 border-steel/40 pl-4">
              <h2 className="mb-1 text-lg font-bold">{d.topic}</h2>
              <p className="leading-relaxed text-ink-soft">{d.text}</p>
              <p className="mt-0.5 text-xs text-ink-faint">出處：{d.source}</p>
            </div>
          ))}
        </section>

        <p className="text-xs text-ink-faint">
          圖紙 ID <code className="font-mono">{p.id}</code> · 示範資料（虛構工廠），非真實企業文件
        </p>
      </div>
    </article>
  );
}
