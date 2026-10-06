import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, assetUrl, type IntakeDraft, type IntakeField } from "../api/client";
import { useAccounts, useIntakeDraft } from "../api/hooks";
import { streamIntake, type IntakeErrorEvent, type IntakeStage } from "../api/sse";
import { ErrorMessage, Loading } from "../components/common/Feedback";
import { ImageUploader } from "../components/common/ImageUploader";

type Domain = "mfg" | "art";

/** 兩個領域只差文字、配色與要不要讀照片；流程、驗證、收錄都同一套（docs/adr/013） */
const DOMAIN = {
  mfg: {
    kicker: "工廠機械加工圖 · 照片建檔",
    title: "拍一張圖紙，建進知識庫",
    intro:
      "本地 Qwen3-VL 讀標題欄與外形尺寸，系統檢查格式、和知識庫有沒有重複、材料依既有零件校正；你對照照片確認、補上照片沒有的欄位，" +
      "主管按「收錄」後寫進知識庫並重建索引，之後拍同一張圖紙就認得出來。圖紙屬企業機密，全程只在本機處理、不送雲端。",
    heading: "t-display",
    tone: "steel" as const,
    labels: { camera: "拍攝圖紙", file: "上傳圖紙", busy: "上傳中…" },
    tips: [
      "把整張圖紙（含下方的標題欄）拍進畫面，背景和紙張顏色要有對比",
      "拿穩、對焦：照片太模糊會請你重拍——模型看不清楚時不會留空，而是猜",
      "目前只收和知識庫圖紙同一種版面（三視圖＋下方標題欄）的圖紙",
    ],
    stages: [
      ["sharpness", "清晰度"],
      ["identify", "知識庫有沒有"],
      ["page", "拉正對齊"],
      ["read", "讀標題欄"],
      ["validate", "驗證"],
    ] as [IntakeStage, string][],
    views: ["拍的照片", "要存進知識庫的圖"],
    other: { label: "圖紙", path: "/drawings/intake" },
  },
  art: {
    kicker: "畫作 · 照片建檔",
    title: "拍一幅畫，建進知識庫",
    intro:
      "拍畫作本身就好（不用拍展牌）：系統先擋掉太模糊的照片、確認知識庫還沒有這幅畫，畫名、作者、年代、典藏與授權等資料在跳出的表單填。" +
      "主管按「收錄」後寫進知識庫並重建索引，之後拍同一幅畫就認得出來。",
    heading: "t-display",
    tone: "seal" as const,
    labels: { camera: "拍攝畫作", file: "上傳照片", busy: "上傳中…" },
    tips: [
      "正面拍、畫面盡量只有畫（這張照片就是之後辨識用的原圖）",
      "拿穩、對焦：照片太模糊會請你重拍，之後也認不出來",
      "資料由你填：館方的展牌解說有著作權，介紹只收 CC0、公有領域、CC BY 4.0",
    ],
    stages: [
      ["sharpness", "清晰度"],
      ["identify", "知識庫有沒有"],
      ["validate", "建立草稿"],
    ] as [IntakeStage, string][],
    views: ["拍的照片", "要存進知識庫的圖"],
    other: { label: "畫作", path: "/artworks/intake" },
  },
};

const SOURCE_STYLE: Record<string, string> = {
  "Qwen3-VL": "bg-accent-soft text-accent",
  規則: "bg-parchment-deep text-ink-80",
  人: "bg-success-soft text-success",
};

/** 收錄後要重新抓的資料：清單、單筆、狀態頁（知識庫版本）、稽核紀錄 */
const STALE = ["parts", "part", "artworks", "artwork", "status", "diagnostics", "audit"];

/**
 * 照片建檔（docs/adr/013）：拍照 → 擋模糊 → 確認知識庫還沒有 → 填欄位 → 主管收錄。
 * 欄位都在跳出的表單確認：圖紙由本地 Qwen3-VL 先填好，人對照照片確認、讀錯就直接改；畫作由人填。
 * 網址帶 ?draft= 就是那份草稿；帶 ?image=（從辨識失敗的頁面過來）就直接開始。
 */
export function IntakePage({ domain }: { domain: Domain }) {
  const cfg = DOMAIN[domain];
  const [params, setParams] = useSearchParams();
  const draftId = params.get("draft");
  const imageParam = params.get("image");
  const [imageId, setImageId] = useState<string | null>(null);
  const [stage, setStage] = useState<IntakeStage | null>(null);
  const [raw, setRaw] = useState("");
  const [failure, setFailure] = useState<IntakeErrorEvent | null>(null);
  const started = useRef<string | null>(null);
  const qc = useQueryClient();

  const start = (id: string) => {
    if (started.current === id) return; // StrictMode 會跑兩次 effect
    started.current = id;
    setImageId(id);
    setStage("sharpness");
    setRaw("");
    setFailure(null);
    streamIntake(id, domain, {
      onStage: (e) => setStage(e.stage),
      onToken: (t) => setRaw((r) => r + t),
      onDraft: (d) => {
        qc.setQueryData(["intake", d.draft_id], d);
        setParams({ draft: d.draft_id }, { replace: true });
      },
      onError: (e) => {
        setFailure(e);
        setStage(null);
      },
    });
  };

  useEffect(() => {
    if (imageParam && !draftId) start(imageParam);
  }, [imageParam, draftId]);

  const reset = () => {
    started.current = null;
    setImageId(null);
    setStage(null);
    setFailure(null);
    setParams({}, { replace: true });
  };

  return (
    <div className="flex flex-col gap-5">
      <header>
        <p className="t-eyebrow">{cfg.kicker}</p>
        <h1 className={`${cfg.heading} mt-1`}>{cfg.title}</h1>
        <p className="mt-1 max-w-3xl text-sm text-ink-80">{cfg.intro}</p>
      </header>

      {draftId ? (
        <DraftView draftId={draftId} domain={domain} onRestart={reset} />
      ) : (
        <section className="grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
          <div className="flex flex-col gap-3">
            {imageId ? (
              <div className="card overflow-hidden p-2">
                <img src={api.uploadedImageUrl(imageId)} alt="拍的照片" className="w-full rounded-lg object-contain" />
              </div>
            ) : (
              <div className="card p-4">
                <ImageUploader tone={cfg.tone} labels={cfg.labels} onUploaded={start} />
                <ul className="mt-3 list-disc pl-5 text-xs text-ink-48">
                  {cfg.tips.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            )}
            {imageId && !stage && (
              <button
                type="button"
                onClick={reset}
                className="btn-ghost self-start"
              >
                換一張
              </button>
            )}
          </div>
          <div className="flex flex-col gap-3">
            {imageId && <StageList stages={cfg.stages} current={stage} failed={!!failure} />}
            {stage === "read" && (
              <div className="card p-3">
                <p className="mb-1 text-xs font-semibold text-ink-48">本地 Qwen3-VL 正在抄寫（這台約 40 秒）</p>
                <pre className="min-h-12 whitespace-pre-wrap break-all font-mono text-xs text-accent">{raw || "…"}</pre>
              </div>
            )}
            {failure && <FailureBox e={failure} imageId={imageId} />}
          </div>
        </section>
      )}
    </div>
  );
}

function StageList({
  stages,
  current,
  failed,
}: {
  stages: [IntakeStage, string][];
  current: IntakeStage | null;
  failed: boolean;
}) {
  const idx = current ? stages.findIndex(([k]) => k === current) : -1;
  return (
    <ol className="flex flex-wrap gap-1.5 text-xs">
      {stages.map(([k, label], i) => {
        const state = i < idx ? "done" : i === idx ? (failed ? "fail" : "now") : "todo";
        return (
          <li
            key={k}
            className={`rounded-full px-2.5 py-1 font-semibold ${
              state === "done"
                ? "bg-success-soft text-success"
                : state === "now"
                  ? "animate-pulse bg-ink text-white"
                  : state === "fail"
                    ? "bg-danger-soft text-danger"
                    : "bg-parchment-deep text-ink-48"
            }`}
          >
            {state === "done" ? "✓ " : ""}
            {label}
          </li>
        );
      })}
    </ol>
  );
}

function FailureBox({ e, imageId }: { e: IntakeErrorEvent; imageId: string | null }) {
  if (e.code === "INTAKE_ALREADY_IN_KB" && (e.part || e.artwork)) {
    const to = e.part ? `/drawings/${e.part.id}` : `/artworks/${e.artwork!.id}`;
    const name = e.part ? e.part.name_zh : e.artwork!.title_zh;
    return (
      <div className="rounded-xl bg-success-soft p-3 text-sm">
        <p className="font-semibold text-success">{e.message}</p>
        <Link to={to} className="link mt-1 inline-block">
          去看〈{name}〉→
        </Link>
      </div>
    );
  }
  if (e.code === "INTAKE_WRONG_DOMAIN" && imageId) {
    const other = e.route?.domain === "mfg" ? DOMAIN.mfg.other : DOMAIN.art.other;
    return (
      <div className="rounded-xl bg-warning-soft p-3 text-sm text-warning">
        <p className="font-semibold">{e.message}</p>
        <Link to={`${other.path}?image=${imageId}`} className="link mt-1 inline-block">
          改用「{other.label}」的拍照建檔 →
        </Link>
      </div>
    );
  }
  return <ErrorMessage title="沒辦法建檔" message={e.message} code={e.code} requestId={e.request_id} />;
}

function SourceChip({ f }: { f: IntakeField }) {
  if (!f.source) return null;
  return (
    <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${SOURCE_STYLE[f.source] ?? ""}`}>
      {f.source}
    </span>
  );
}

const shown = (v: IntakeField["value"]) => (v === null || v === undefined ? "" : String(v));
const CODE_KEYS = ["part_no", "drawing_no", "revision", "id_code", "id_no"];

/** 欄位輸入：圖紙在頁面上、畫作在跳出的表單裡共用；有 group 就分區 */
function FieldsForm({
  fields,
  edits,
  setEdits,
  editable,
}: {
  fields: IntakeField[];
  edits: Record<string, string>;
  setEdits: (fn: (e: Record<string, string>) => Record<string, string>) => void;
  editable: boolean;
}) {
  const groups: [string | null, IntakeField[]][] = [];
  for (const f of fields) {
    const last = groups[groups.length - 1];
    if (last && last[0] === (f.group ?? null)) last[1].push(f);
    else groups.push([f.group ?? null, [f]]);
  }
  return (
    <div className="flex flex-col gap-4">
      {groups.map(([group, fs]) => (
        <fieldset key={group ?? "fields"} className="flex flex-col gap-2">
          {group && <legend className="mb-1 text-sm font-semibold text-ink">{group}</legend>}
          <div className="grid gap-x-3 gap-y-2.5 sm:grid-cols-2">
            {fs.map((f) => {
              const value = edits[f.key] ?? shown(f.value);
              const bad = (f.status === "invalid" || f.status === "missing") && !(f.key in edits);
              const listId = f.suggestions.length ? `intake-${f.key}` : undefined;
              const set = (v: string) => setEdits((e) => ({ ...e, [f.key]: v }));
              const cls = `w-full rounded-lg border bg-white px-2 py-1.5 text-sm text-ink outline-none focus:border-accent disabled:bg-parchment-deep ${
                bad ? "border-danger" : "border-hairline"
              } ${f.kind === "number" || CODE_KEYS.includes(f.key) ? "font-mono" : ""}`;
              return (
                <label
                  key={f.key}
                  className={`flex flex-col gap-0.5 text-xs text-ink-48 ${f.kind === "longtext" ? "sm:col-span-2" : ""}`}
                >
                  <span className="flex items-center gap-1.5">
                    {f.label}
                    {f.required && "＊"}
                    <SourceChip f={f} />
                  </span>
                  {f.kind === "enum" ? (
                    <select value={value} disabled={!editable} onChange={(e) => set(e.target.value)} className={cls}>
                      <option value="">（請選擇）</option>
                      {f.options.map((o) => (
                        <option key={o}>{o}</option>
                      ))}
                    </select>
                  ) : f.kind === "longtext" ? (
                    <textarea
                      value={value}
                      disabled={!editable}
                      rows={4}
                      placeholder={f.hint ?? undefined}
                      onChange={(e) => set(e.target.value)}
                      className={cls}
                    />
                  ) : (
                    <input
                      value={value}
                      disabled={!editable}
                      type={f.kind === "url" ? "url" : "text"}
                      inputMode={f.kind === "number" ? "decimal" : undefined}
                      placeholder={f.hint ?? undefined}
                      list={listId}
                      onChange={(e) => set(e.target.value)}
                      className={cls}
                    />
                  )}
                  {listId && (
                    <datalist id={listId}>
                      {f.suggestions.map((s) => (
                        <option key={s} value={s} />
                      ))}
                    </datalist>
                  )}
                  {f.message && !(f.key in edits) && <span className="text-danger">{f.message}</span>}
                  {f.note && !(f.key in edits) && <span className="text-warning">{f.note}</span>}
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}

/** 表單關掉之後的摘要；圖紙另外標每格的來源（模型讀的、規則校正的、人改的） */
function FieldSummary({ fields, showSource }: { fields: IntakeField[]; showSource?: boolean }) {
  const filled = fields.filter((f) => f.value !== null && f.value !== "");
  return (
    <dl className="grid grid-cols-[6.5em_1fr] gap-x-3 gap-y-1 text-sm">
      {filled.map((f) => (
        <div key={f.key} className="contents">
          <dt className="text-ink-48">{f.label}</dt>
          <dd className={`flex flex-wrap items-center gap-1.5 break-words ${f.status === "invalid" ? "text-danger" : ""}`}>
            <span className={f.kind === "number" || CODE_KEYS.includes(f.key) ? "font-mono" : ""}>{shown(f.value)}</span>
            {showSource && <SourceChip f={f} />}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** 跳出的表單（原生 <dialog>：Esc 可以關、背景不能點）；aside 放在表單旁邊（圖紙的照片，對照著確認） */
function FormDialog({
  open,
  title,
  onClose,
  aside,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  aside?: ReactNode;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-label={title}
      className={`m-auto w-[calc(100%-2rem)] ${aside ? "max-w-5xl" : "max-w-2xl"} rounded-2xl border border-hairline bg-card p-0 text-ink backdrop:bg-ink/40`}
    >
      <div className="flex max-h-[88vh] flex-col">
        <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
          <h2 className="t-tagline">{title}</h2>
          <button type="button" onClick={onClose} aria-label="關閉" className="rounded-lg px-2 py-1 text-ink-48 hover:bg-parchment-deep">
            ✕
          </button>
        </div>
        {aside ? (
          <div className="grid gap-4 overflow-y-auto px-4 py-3 md:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
            <div className="md:sticky md:top-0 md:self-start">{aside}</div>
            <div>{children}</div>
          </div>
        ) : (
          <div className="overflow-y-auto px-4 py-3">{children}</div>
        )}
      </div>
    </dialog>
  );
}

function DraftView({ draftId, domain, onRestart }: { draftId: string; domain: Domain; onRestart: () => void }) {
  const cfg = DOMAIN[domain];
  const { data: d, isLoading, error } = useIntakeDraft(draftId);
  const me = useAccounts().data?.current;
  const qc = useQueryClient();
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [view, setView] = useState<0 | 1>(0);
  const [formOpen, setFormOpen] = useState(false);
  const autoOpened = useRef(false);

  // 收錄完成：清單、知識庫版本都變了
  const status = d?.status;
  useEffect(() => {
    if (status === "done") STALE.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  }, [status, qc]);

  // 草稿一建好就跳出表單（只自動開一次）：圖紙是模型填好的欄位，一律要人確認；畫作還有沒填的才開
  const isArt = d?.domain === "art";
  useEffect(() => {
    if (d?.status === "draft" && (!isArt || d.blockers.length > 0) && !autoOpened.current) {
      autoOpened.current = true;
      setFormOpen(true);
    }
  }, [isArt, d?.status, d?.blockers.length]);

  if (isLoading) return <Loading />;
  if (error || !d)
    return (
      <div className="flex flex-col items-start gap-3">
        <ErrorMessage
          title="找不到這份建檔草稿"
          message={(error as Error)?.message ?? ""}
          requestId={(error as ApiError)?.requestId}
        />
        <button type="button" onClick={onRestart} className="link">
          重新拍一張 →
        </button>
      </div>
    );

  const editable = d.status === "draft" || d.status === "failed";
  const dirty = Object.keys(edits).length > 0;
  const canCommit = me?.ops.includes("kb_intake") ?? false;
  // 摘要要不要顯示：畫作看人填過沒（規則一開始就帶了 ID），圖紙看有沒有任何欄位有值
  const filled = isArt
    ? d.fields.some((f) => f.source === "人")
    : d.fields.some((f) => f.value !== null && f.value !== "");

  const apply = (next: IntakeDraft) => {
    qc.setQueryData(["intake", draftId], next);
    setEdits({});
    return next;
  };
  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    setActionError(null);
    try {
      await fn();
    } catch (e) {
      setActionError(e instanceof ApiError ? `${e.message}（${e.code}）` : String(e));
    } finally {
      setBusy(null);
    }
  };
  const saveEdits = async () => {
    const values = Object.fromEntries(
      Object.entries(edits).map(([k, v]) => {
        const f = d.fields.find((x) => x.key === k)!;
        if (v.trim() === "") return [k, null];
        return [k, f.kind === "number" ? Number(v) : v];
      }),
    );
    return apply(await api.updateIntake(draftId, values));
  };
  // 跳出的表單：存好且沒有要處理的欄位就關掉；還有沒過的就留著，紅字標在欄位下面
  const saveDialog = () =>
    run("save", async () => {
      const next = dirty ? await saveEdits() : d;
      if (next.blockers.length === 0) setFormOpen(false);
    });
  const commit = () =>
    run("commit", async () => {
      if (dirty) await saveEdits(); // 沒儲存的修改先存，驗證沒過後端會擋
      apply(await api.commitIntake(draftId));
    });
  const discard = () =>
    run("discard", async () => {
      await api.discardIntake(draftId);
      qc.removeQueries({ queryKey: ["intake", draftId] });
      onRestart();
    });
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    saveDialog();
  };

  const form = (
    <form onSubmit={onSubmit} className="flex flex-col gap-3">
      <FieldsForm fields={d.fields} edits={edits} setEdits={setEdits} editable={editable} />
      {editable && (
        <div className="flex flex-wrap items-center gap-2">
          <button disabled={!!busy} className="btn-primary">
            {busy === "save" ? "儲存中…" : isArt ? "儲存資料" : dirty ? "儲存修改" : "確認無誤"}
          </button>
          {dirty && <span className="text-xs text-warning">有修改還沒儲存</span>}
          {d.blockers.length > 0 && !dirty && (
            <span className="text-xs text-danger">還要處理：{d.blockers.join("、")}</span>
          )}
        </div>
      )}
    </form>
  );

  return (
    <article className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <div className="flex flex-col gap-2 lg:sticky lg:top-20 lg:self-start">
        {domain === "mfg" && (
          <div className="flex rounded-full border border-hairline bg-card p-1 text-sm font-normal">
            {cfg.views.map((label, k) => (
              <button
                key={label}
                type="button"
                onClick={() => setView(k as 0 | 1)}
                className={`flex-1 rounded-full px-3 py-1.5 transition ${
                  view === k ? "bg-accent text-white" : "text-ink-80 hover:bg-parchment-deep"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        <div className="card overflow-hidden p-2">
          <img
            src={assetUrl(view === 0 ? d.photo_url : d.kb_image_url)}
            alt={cfg.views[view]}
            className="w-full rounded-lg object-contain"
          />
        </div>
        <p className="text-xs text-ink-48">
          {domain === "art"
            ? "這張照片會存成知識庫的原圖（長邊 1024 px），之後辨識、色彩分析都用它"
            : view === 0
              ? "對照照片確認每個欄位：模型讀的（藍）、系統依知識庫校正的（灰）、你填的（綠）"
              : "拉正、對齊知識庫圖紙版面、去陰影後的樣子；收錄後辨識與 3D 重建都用這張"}
        </p>
      </div>

      <div className="flex flex-col gap-4">
        <ul className="grid gap-1 text-sm sm:grid-cols-2">
          {d.checks.map((c) => (
            <li
              key={c.label}
              className={`flex items-start gap-2 rounded-lg px-2 py-1.5 ${c.ok ? "bg-success-soft" : "bg-danger-soft"}`}
            >
              <span
                className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full text-[10px] font-semibold text-white ${
                  c.ok ? "bg-success" : "bg-danger"
                }`}
              >
                {c.ok ? "✓" : "✕"}
              </span>
              <span>
                <b className="font-semibold">{c.label}</b> <span className="text-ink-80">{c.detail}</span>
              </span>
            </li>
          ))}
        </ul>

        <section className="card p-4">
          <div className="mb-3 flex items-baseline justify-between gap-2">
            <h2 className="font-semibold">
              {isArt ? "畫作資料" : "圖紙資料"} <span className="font-mono text-sm text-ink-48">{d.item_id}</span>
            </h2>
            {editable && (
              <button type="button" onClick={() => setFormOpen(true)} className="btn-ghost px-4 py-1.5 text-sm">
                {!isArt ? "確認／修改資料" : filled ? "編輯資料" : "填寫畫作資料"}
              </button>
            )}
          </div>
          {filled ? (
            <FieldSummary fields={d.fields} showSource={!isArt} />
          ) : (
            <p className="text-sm text-ink-80">
              還沒填資料：按「{isArt ? "填寫畫作資料" : "確認／修改資料"}」。
            </p>
          )}
          {d.blockers.length > 0 && <p className="mt-2 text-sm text-danger">還要處理：{d.blockers.join("、")}</p>}
          {dirty && !formOpen && (
            <p className="mt-2 text-sm text-warning">表單裡有修改還沒儲存：按「{isArt ? "編輯資料" : "確認／修改資料"}」回去儲存</p>
          )}
          <FormDialog
            open={formOpen}
            title={isArt ? "畫作資料" : "圖紙資料"}
            onClose={() => setFormOpen(false)}
            aside={
              isArt ? undefined : (
                <figure className="flex flex-col gap-1">
                  <a href={assetUrl(d.photo_url)} target="_blank" rel="noreferrer" title="開新分頁看原尺寸">
                    <img src={assetUrl(d.photo_url)} alt="拍的照片" className="w-full rounded-lg object-contain" />
                  </a>
                  <figcaption className="text-xs text-ink-48">拍的照片（點一下開新分頁看原尺寸）</figcaption>
                </figure>
              )
            }
          >
            <p className="mb-3 text-xs text-ink-48">
              {isArt
                ? "＊必填。典藏單位是知識庫已有的，來源代碼會自動帶入；沒填介紹，系統會依這些欄位寫一段「基本資料」。"
                : "＊必填。欄位已由本地 Qwen3-VL 讀照片自動填好（藍）、系統依知識庫校正（灰）；對照左邊的照片確認，讀錯的直接改（改過標綠），照片上沒有的欄位請補上。"}
            </p>
            {form}
          </FormDialog>
        </section>

        <CommitBox
          d={d}
          meLabel={me?.label}
          canCommit={canCommit}
          busy={busy}
          onCommit={commit}
          onDiscard={discard}
          onRestart={onRestart}
        />
        {actionError && <p className="rounded-lg bg-danger-soft p-2 text-sm text-danger">{actionError}</p>}

        {d.extraction.raw && (
          <details className="text-xs text-ink-48">
            <summary className="cursor-pointer">模型原始輸出（{d.extraction.model}）</summary>
            <pre className="mt-1 whitespace-pre-wrap break-all rounded-lg bg-parchment-deep p-2 font-mono">{d.extraction.raw}</pre>
          </details>
        )}
        <p className="text-xs text-ink-48">
          草稿 <code className="font-mono">{d.draft_id}</code> · 保存 7 天 · 外送 {d.egress.images} 張圖、
          {d.egress.bytes} bytes
        </p>
      </div>
    </article>
  );
}

function CommitBox({
  d,
  meLabel,
  canCommit,
  busy,
  onCommit,
  onDiscard,
  onRestart,
}: {
  d: IntakeDraft;
  meLabel?: string;
  canCommit: boolean;
  busy: string | null;
  onCommit: () => void;
  onDiscard: () => void;
  onRestart: () => void;
}) {
  const noun = d.domain === "art" ? "這幅畫" : "這張圖紙";
  if (d.status === "indexing")
    return (
      <div className="rounded-xl bg-accent-soft p-3 text-sm text-accent">
        <p className="animate-pulse font-semibold">已寫進知識庫，正在重建索引…</p>
        <p className="text-xs">知識庫版本 {d.commit?.kb_version}；重建完成前，以圖搜圖還認不出{noun}</p>
      </div>
    );
  if (d.status === "done")
    return (
      <div className="rounded-xl bg-success-soft p-3 text-sm">
        <p className="font-semibold text-success">
          ✓ 已收錄為 {d.item_id}（知識庫版本 {d.commit?.kb_version}，索引 {((d.commit?.index_ms ?? 0) / 1000).toFixed(0)}{" "}
          秒）
        </p>
        <div className="mt-1 flex flex-wrap gap-x-4">
          {d.item_url && (
            <Link to={d.item_url} className="link">
              去看{noun} →
            </Link>
          )}
          <button type="button" onClick={onRestart} className="link">
            再建一筆 →
          </button>
        </div>
      </div>
    );
  return (
    <div className="flex flex-col gap-2">
      {d.status === "failed" && d.commit?.error && (
        <ErrorMessage title="收錄失敗，寫進去的檔案與版本已還原" message={d.commit.error} />
      )}
      {!canCommit && (
        <div className="rounded-xl bg-warning-soft p-3 text-sm text-warning">
          目前身分「{meLabel ?? "訪客"}」不能收錄，請在頁首切換成「主管」。草稿會保留，切換後回到這頁即可。
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={!canCommit || !d.can_commit || !!busy}
          onClick={onCommit}
          className="btn-primary"
        >
          {busy === "commit" ? "收錄中…" : `收錄進知識庫（${d.item_id}）`}
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={onDiscard}
          className="btn-ghost"
        >
          {busy === "discard" ? "捨棄中…" : "捨棄草稿"}
        </button>
      </div>
    </div>
  );
}
