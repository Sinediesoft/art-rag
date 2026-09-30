import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, assetUrl, type CadStrategy } from "../../api/client";
import { useHealth, usePart } from "../../api/hooks";
import type { Dims } from "../../api/sse";
import { ErrorMessage } from "../../components/common/Feedback";
import { ImageUploader } from "../../components/common/ImageUploader";
import { EgressBadge } from "../../components/common/StatusNotices";
import { ModelViewer, type ModelLayer } from "../../components/LazyModelViewer";
import { ConfidentialityBadge, ScoreMeter } from "../../components/parts/PartBadges";
import { useReconstruct, type CadStatus } from "../../hooks/useReconstruct";
import { seconds } from "../../lib/format";

const MODELS: { value: CadStrategy; label: string; hint: string }[] = [
  { value: "ortho2cad", label: "Ortho2CAD", hint: "Qwen3-VL-8B 以三視圖→CadQuery 微調（主模型）" },
  { value: "hybrid", label: "Qwen3-VL 4B 未微調", hint: "對照組：同一張圖給一般 VLM 寫 CadQuery" },
];

const LAYOUT: Record<string, string> = {
  kb: "知識庫圖紙：裁掉標題欄，只留三視圖",
  rectified: "照片已辨識，依 homography 拉正並二值化",
  upload: "未收錄圖紙：自動裁切、補白成正方形並二值化",
};

/** /drawings/:id/reconstruct（知識庫圖紙，可帶 ?image= 照片或 ?job= 已完成的結果）與 /reconstruct?image= */
export function ReconstructPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const imageId = params.get("image");
  const jobId = params.get("job");
  const part = usePart(id);
  const { data: health } = useHealth();
  const { state, start, load } = useReconstruct();
  const [strategy, setStrategy] = useState<CadStrategy>("ortho2cad");
  const [now, setNow] = useState(Date.now());
  const running = (["preparing", "generating", "executing"] as CadStatus[]).includes(state.status);

  useEffect(() => {
    if (jobId) void load(jobId);
    else if (id || imageId) start({ part_id: id ?? null, image_id: imageId, strategy: "ortho2cad" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, imageId, jobId]);

  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [running]);

  const run = (s: CadStrategy) => {
    setStrategy(s);
    if (jobId) navigate(id ? `/drawings/${id}/reconstruct` : `/reconstruct`, { replace: true });
    start({ part_id: id ?? null, image_id: imageId, strategy: s });
  };

  if (!id && !imageId && !jobId) return <UploadPrompt />;

  const { meta, result, done, error, code } = state;
  const p = meta?.part ?? null;
  const shownStrategy = meta?.strategy ?? strategy;
  const inputUrl = imageId
    ? api.uploadedImageUrl(imageId)
    : meta?.image_id
      ? api.uploadedImageUrl(meta.image_id)
      : p
        ? assetUrl(p.drawing_url)
        : part.data
          ? assetUrl(part.data.drawing_url)
          : null;
  const elapsed = running ? now - state.startedAt : (done?.latency_ms.total ?? 0);
  const ortho = health?.strategies.ortho2cad;

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-xs text-ink-faint">
            <Link to={id ? `/drawings/${id}` : "/drawings"} className="hover:text-steel">
              ← {id ? "圖紙資料" : "工廠圖紙"}
            </Link>
          </p>
          <h1 className="text-2xl font-black sm:text-3xl">三視圖 → 3D 模型</h1>
          <p className="flex flex-wrap items-center gap-2 text-sm text-ink-soft">
            {p ? (
              <>
                {p.name_zh} · {p.part_no} · {p.drawing_no} rev.{p.revision}
                <ConfidentialityBadge level={p.confidentiality} />
              </>
            ) : meta ? (
              "未收錄的圖紙（沒有標準模型可比對）"
            ) : (
              part.data?.name.zh ?? "準備中…"
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-xl border border-line bg-card p-1">
            {MODELS.map((m) => (
              <button
                key={m.value}
                type="button"
                title={m.hint}
                disabled={running}
                onClick={() => run(m.value)}
                className={`rounded-lg px-3 py-1 text-sm font-medium transition disabled:opacity-60 ${
                  shownStrategy === m.value ? "bg-steel text-white" : "text-ink-soft hover:bg-paper-deep"
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={running}
            onClick={() => run(shownStrategy as CadStrategy)}
            className="rounded-xl border border-ink/15 bg-card px-3 py-1.5 text-sm font-bold transition hover:border-ink/40 disabled:opacity-50"
          >
            {running ? `產生中 ${(elapsed / 1000).toFixed(0)} 秒` : "重新產生"}
          </button>
        </div>
      </header>

      {ortho && !ortho.available && shownStrategy === "ortho2cad" && (
        <div className="rounded-xl border border-amber/30 bg-amber-soft/60 p-3 text-sm text-amber">
          Ortho2CAD 推論伺服器未啟動（{ortho.detail}）。在終端機執行 <code className="font-mono">make ortho2cad</code>。
        </div>
      )}

      <Pipeline state={state.status} hasMeta={!!meta} result={result} cached={state.cached} />

      {error && (
        <ErrorMessage
          title={error.code === "STRATEGY_UNAVAILABLE" ? "推論伺服器無法使用" : "3D 重建失敗"}
          message={error.message}
          code={error.code}
          requestId={error.request_id}
        />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-4">
          <Card title="輸入圖紙">
            {inputUrl ? (
              <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
                <img src={inputUrl} alt="輸入圖紙" className="w-full rounded-lg border border-line bg-white object-contain" />
                {meta && (
                  <figure className="w-[112px] text-center">
                    <img
                      src={assetUrl(meta.input_url)}
                      alt="模型看到的影像"
                      className="pixelated w-[112px] rounded border border-line bg-white"
                    />
                    <figcaption className="mt-1 text-[11px] leading-tight text-ink-faint">
                      模型實際看到的
                      <br />
                      {meta.input_size[0]}×{meta.input_size[1]}
                    </figcaption>
                  </figure>
                )}
              </div>
            ) : (
              <p className="text-sm text-ink-faint">載入中…</p>
            )}
            {meta && (
              <ul className="mt-3 flex flex-col gap-1 text-xs text-ink-soft">
                <li>• {LAYOUT[meta.layout]}</li>
                <li>
                  • 尺寸依據：
                  {meta.scale_to
                    ? `${meta.scale_source}（${meta.scale_to.width} × ${meta.scale_to.depth} × ${meta.scale_to.height} mm）`
                    : "沒有讀到尺寸標註，顯示模型原始尺度"}
                </li>
              </ul>
            )}
          </Card>

          {result?.ok && result.files["model.stl"] && (
            <ModelCard stl={result.files["model.stl"]} gt={p?.model_url ?? null} />
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <CodePanel code={code} status={state.status} model={meta?.model} files={result?.files} />
          {result && <ResultCard result={result} target={meta?.scale_to ?? null} gt={p?.geometry ?? null} />}
          {done && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-faint">
              <EgressBadge egress={done.egress} />
              <span className="font-medium text-ink-soft">
                {done.strategy === "ortho2cad" ? "Ortho2CAD" : "Qwen3-VL 對照組"} · {done.model}
              </span>
              <span>首字 {seconds(done.latency_ms.first_token)}</span>
              <span>生成 {seconds(done.latency_ms.generation)}</span>
              <span>執行 {seconds(done.latency_ms.exec)}</span>
              <span>總計 {seconds(done.latency_ms.total)}</span>
              <span>{done.tokens.output} tokens</span>
              {state.cached && <span className="rounded bg-paper-deep px-1.5">已完成的結果</span>}
            </div>
          )}
        </div>
      </div>

      {result?.ok && inputUrl && (
        <Card title="回投影比對：重建模型再畫一次三視圖">
          <div className="grid gap-3 sm:grid-cols-2">
            <figure>
              <img
                src={meta?.layout === "kb" && p ? assetUrl(p.drawing_url) : inputUrl}
                alt="輸入圖紙"
                className="aspect-square w-full rounded-lg border border-line bg-white object-cover object-top"
              />
              <figcaption className="mt-1 text-center text-xs text-ink-faint">輸入圖紙</figcaption>
            </figure>
            <figure>
              <img
                src={assetUrl(result.files["reproj.png"])}
                alt="重建模型的三視圖"
                className="aspect-square w-full rounded-lg border border-line bg-white object-contain"
              />
              <figcaption className="mt-1 text-center text-xs text-ink-faint">重建模型的三視圖（同一套產生器）</figcaption>
            </figure>
          </div>
        </Card>
      )}
    </div>
  );
}

function Card({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="rounded-2xl border border-line bg-card p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-bold">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

function Pipeline({
  state,
  hasMeta,
  result,
  cached,
}: {
  state: CadStatus;
  hasMeta: boolean;
  result: { ok: boolean } | null;
  cached: boolean;
}) {
  const order: CadStatus[] = ["preparing", "generating", "executing", "done"];
  const at = state === "error" ? (hasMeta ? (result ? 3 : 1) : 0) : Math.max(0, order.indexOf(state));
  const steps = ["圖紙前處理・讀尺寸", "Ortho2CAD 產生 CadQuery", "沙箱執行", "3D 模型"];
  return (
    <ol className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {steps.map((label, i) => {
        const failed = state === "error" && i === at;
        const finished = cached || i < at || (state === "done" && (i < 3 || !!result?.ok));
        const active = !cached && i === at && state !== "done" && state !== "error";
        return (
          <li
            key={label}
            className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-sm ${
              failed
                ? "border-seal/40 bg-seal-soft text-seal-deep"
                : finished
                  ? "border-jade/30 bg-jade-soft/60 text-jade"
                  : active
                    ? "border-steel/40 bg-steel-soft text-steel-deep"
                    : "border-line bg-card text-ink-faint"
            }`}
          >
            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-white/80 text-[11px] font-bold">
              {failed ? "✗" : finished ? "✓" : active ? <Spinner /> : i + 1}
            </span>
            <span className="leading-tight">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

const Spinner = () => <span className="h-3 w-3 animate-spin rounded-full border-2 border-steel/30 border-t-steel" />;

function CodePanel({
  code,
  status,
  model,
  files,
}: {
  code: string;
  status: CadStatus;
  model?: string;
  files?: Record<string, string>;
}) {
  const ref = useRef<HTMLPreElement>(null);
  const streaming = status === "generating";
  useEffect(() => {
    if (streaming && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [code, streaming]);
  const lines = code ? code.split("\n").length : 0;
  return (
    <Card
      title="CadQuery 程式碼"
      right={
        <span className="flex items-center gap-2 text-xs text-ink-faint">
          {model && <span className="font-mono">{model}</span>}
          {lines > 0 && <span>{lines} 行</span>}
          {files?.["code.py"] && (
            <a href={assetUrl(files["code.py"])} className="text-steel underline">
              下載 .py
            </a>
          )}
        </span>
      }
    >
      <pre
        ref={ref}
        className={`max-h-[460px] min-h-[220px] overflow-auto rounded-xl bg-code p-3 font-mono text-[12px] leading-relaxed text-[#d7e3ee] ${
          streaming ? "caret" : ""
        }`}
      >
        {code ||
          (status === "preparing"
            ? "前處理圖紙中…"
            : status === "generating"
              ? "等待 Ortho2CAD 回應…"
              : "")}
      </pre>
      {status === "executing" && (
        <p className="mt-2 flex items-center gap-2 text-sm text-steel-deep">
          <Spinner /> 安全檢查通過，在沙箱中執行程式碼（禁止網路、只准寫入暫存目錄）…
        </p>
      )}
    </Card>
  );
}

function ResultCard({
  result,
  target,
  gt,
}: {
  result: {
    ok: boolean;
    error: string | null;
    valid: boolean | null;
    repaired: boolean | null;
    raw_dims: Dims | null;
    dims: Dims | null;
    scale: number | null;
    volume: number | null;
    iou: number | null;
    iou_bbox: number | null;
    files: Record<string, string>;
  };
  target: Dims | null;
  gt: { volume_mm3: number } | null;
}) {
  if (!result.ok)
    return (
      <div className="rounded-2xl border border-seal/30 bg-seal-soft/50 p-4 text-sm">
        <p className="font-bold text-seal-deep">程式碼無法產生實體</p>
        <p className="mt-1 break-all font-mono text-xs text-ink-soft">{result.error}</p>
        <p className="mt-2 text-xs text-ink-faint">模型輸出不保證可執行；論文中未微調模型的可執行率遠低於 Ortho2CAD。</p>
      </div>
    );
  const d = result.dims!;
  const axes: [keyof Dims, string][] = [
    ["width", "寬 X"],
    ["depth", "深 Y"],
    ["height", "高 Z"],
  ];
  return (
    <Card
      title="重建結果"
      right={
        <span className="flex gap-2 text-xs">
          <a href={assetUrl(result.files["model.step"])} className="text-steel underline">
            STEP
          </a>
          <a href={assetUrl(result.files["model.stl"])} className="text-steel underline">
            STL
          </a>
        </span>
      }
    >
      <div className="flex flex-col gap-3 text-sm">
        <p className="flex flex-wrap gap-2">
          <span className="rounded-full bg-jade-soft px-2 py-0.5 text-xs font-bold text-jade">✓ 程式碼可執行</span>
          <span
            title={result.repaired ? "模型產生的實體有面方向或縫隙等瑕疵，已用 OCC ShapeFix 自動修復" : ""}
            className={`rounded-full px-2 py-0.5 text-xs font-bold ${
              result.valid ? "bg-jade-soft text-jade" : "bg-amber-soft text-amber"
            }`}
          >
            {result.valid ? "✓ 實體有效" : result.repaired ? "實體有瑕疵 → 已自動修復" : "實體有瑕疵"}
          </span>
        </p>
        {result.iou != null && (
          <div className="flex flex-col gap-1.5">
            <ScoreMeter label="IoU" value={result.iou} />
            {result.iou_bbox != null && <ScoreMeter label="外框對齊" value={result.iou_bbox} />}
            <p className="text-xs text-ink-faint">
              與知識庫標準模型的體積交聯比。IoU：對齊質心與慣性主軸、正規化尺度（Ortho2CAD 論文的評估法，論文在
              DeepCAD 測試集平均 0.79；對薄壁件很嚴格）。外框對齊：把重建結果的外框對齊到圖紙標註的外形尺寸，只看形狀像不像。
            </p>
          </div>
        )}
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-ink-faint">
            <tr>
              <th className="py-1 font-medium" />
              {target && <th className="font-medium">圖紙標註</th>}
              <th className="font-medium">重建結果</th>
              {target && <th className="font-medium">誤差</th>}
            </tr>
          </thead>
          <tbody className="font-mono tabular-nums">
            {axes.map(([k, label]) => (
              <tr key={k} className="border-t border-line">
                <td className="py-1 font-sans text-ink-faint">{label}</td>
                {target && <td>{target[k].toFixed(1)}</td>}
                <td>{d[k].toFixed(1)}</td>
                {target && (
                  <td className={Math.abs(d[k] - target[k]) / target[k] > 0.1 ? "text-amber" : "text-jade"}>
                    {(((d[k] - target[k]) / target[k]) * 100).toFixed(1)}%
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-ink-faint">
          模型輸出為 DeepCAD 正規化尺度（原始外形 {result.raw_dims!.width.toFixed(3)} × {result.raw_dims!.depth.toFixed(3)} ×{" "}
          {result.raw_dims!.height.toFixed(3)}），依圖紙標註等比縮放 ×{result.scale!.toFixed(2)}，單位 mm。
          {gt && result.volume != null && (
            <>
              {" "}
              體積 {Math.round(result.volume).toLocaleString()} mm³（標準模型 {Math.round(gt.volume_mm3).toLocaleString()}）。
            </>
          )}
        </p>
      </div>
    </Card>
  );
}

function ModelCard({ stl, gt }: { stl: string; gt: string | null }) {
  const [mode, setMode] = useState<"gen" | "overlay" | "gt">("gen");
  const layers: ModelLayer[] =
    mode === "gen" || !gt
      ? [{ url: stl, color: "#8fb3d6" }]
      : mode === "gt"
        ? [{ url: gt, color: "#c9b79c" }]
        : [
            { url: stl, color: "#8fb3d6" },
            { url: gt, color: "#b3261e", ghost: true },
          ];
  return (
    <Card
      title="3D 模型"
      right={
        gt && (
          <div className="flex rounded-lg border border-line p-0.5 text-xs">
            {(
              [
                ["gen", "重建結果"],
                ["overlay", "疊合比較"],
                ["gt", "標準模型"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                onClick={() => setMode(k)}
                className={`rounded-md px-2 py-0.5 ${mode === k ? "bg-steel text-white" : "text-ink-soft"}`}
              >
                {label}
              </button>
            ))}
          </div>
        )
      }
    >
      <ModelViewer layers={layers} height={340} />
      {mode === "overlay" && (
        <p className="mt-2 text-xs text-ink-faint">藍色實體＝Ortho2CAD 重建結果；紅色半透明＝知識庫標準模型（以外框中心對齊）</p>
      )}
    </Card>
  );
}

function UploadPrompt() {
  const navigate = useNavigate();
  return (
    <div className="blueprint mx-auto flex max-w-xl flex-col gap-4 rounded-2xl border border-steel/20 p-6">
      <h1 className="text-2xl font-black">三視圖 → 3D 模型</h1>
      <p className="text-ink-soft">
        上傳第一角法三視圖（前視、俯視、右視，隱藏線以虛線表示）。已收錄的圖紙會先辨識並拉正；
        未收錄的圖紙由本地 Qwen3-VL 讀取尺寸標註，Ortho2CAD 建模。
      </p>
      <ImageUploader
        tone="steel"
        labels={{ camera: "拍攝圖紙", file: "上傳圖紙" }}
        onUploaded={(imageId) => navigate(`/reconstruct?image=${imageId}`)}
      />
    </div>
  );
}
