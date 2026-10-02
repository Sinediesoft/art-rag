import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useHealth, useParts } from "../../api/hooks";
import { ApiErrorMessage, Loading } from "../../components/common/Feedback";
import { ImageUploader } from "../../components/common/ImageUploader";
import { PartCard } from "../../components/parts/PartCard";

const EXAMPLES = ["有 6 個螺栓孔的法蘭", "需要高週波淬火的零件", "鑄鐵材質的零件", "裝 NEMA 23 馬達用的板子"];

const STEPS = [
  { title: "辨識圖紙", body: "Chinese-CLIP 粗篩 → ORB 幾何驗證 → 拉正後比對線條重合度" },
  { title: "Ortho2CAD 3D 重建", body: "微調的 Qwen3-VL-8B 讀三視圖，寫出 CadQuery 程式碼，在沙箱執行成 3D 模型" },
  { title: "製程問答", body: "從製程、公差與檢驗規範檢索段落，本地 Qwen3-VL 回答並標註出處" },
];

export function DrawingsHomePage() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const parts = useParts();
  const { data: health } = useHealth();
  const ortho = health?.strategies.ortho2cad;

  const submit = (e?: FormEvent, text = q) => {
    e?.preventDefault();
    if (text.trim()) navigate(`/drawings/search?q=${encodeURIComponent(text.trim())}`);
  };

  return (
    <div className="flex flex-col gap-8">
      <section className="blueprint relative overflow-hidden rounded-2xl border border-steel/20 p-5 shadow-sm sm:p-8">
        <p className="text-sm font-bold tracking-widest text-steel">工廠機械加工圖 · 地端多模態 RAG</p>
        <h1 className="mt-1 text-3xl font-black leading-tight sm:text-4xl">
          拍下一張加工圖，
          <br className="sm:hidden" />
          還原成 3D、查到製程
        </h1>
        <p className="mt-2 max-w-2xl text-ink-soft">
          辨識是哪一張圖紙，用 <b className="text-steel-deep">Ortho2CAD</b> 把三視圖轉成可編輯的 CadQuery 程式碼與 3D
          模型，再從製程與檢驗規範回答問題。圖紙屬企業機密，全程只在本機處理、不送雲端。
        </p>
        <div className="mt-5 max-w-md">
          <ImageUploader
            tone="steel"
            labels={{ camera: "拍攝圖紙", file: "上傳圖紙" }}
            onUploaded={(id) => navigate(`/drawings/search?image=${id}`)}
          />
        </div>

        <form onSubmit={submit} className="mt-5 flex max-w-xl gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="或用文字找圖紙，例如「鑄鐵軸承座」"
            className="min-w-0 flex-1 rounded-xl border border-steel/20 bg-white/90 px-4 py-3 outline-none transition focus:border-steel"
          />
          <button className="shrink-0 rounded-xl bg-ink px-4 py-3 font-bold text-paper transition hover:bg-ink/85">
            找圖紙
          </button>
        </form>
        <div className="mt-3 flex flex-wrap gap-2">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => submit(undefined, ex)}
              className="rounded-full border border-steel/20 bg-white/80 px-3 py-1 text-sm text-ink-soft transition hover:border-steel hover:text-steel"
            >
              {ex}
            </button>
          ))}
        </div>
      </section>

      <section className="grid gap-3 sm:grid-cols-3">
        {STEPS.map((s, i) => (
          <div key={s.title} className="rounded-xl border border-line bg-card p-4">
            <p className="font-mono text-xs font-bold text-steel">0{i + 1}</p>
            <p className="font-bold">{s.title}</p>
            <p className="mt-1 text-sm text-ink-soft">{s.body}</p>
          </div>
        ))}
      </section>

      {ortho && !ortho.available && (
        <div className="rounded-xl border border-amber/30 bg-amber-soft/60 p-3 text-sm text-amber">
          Ortho2CAD 推論伺服器未啟動：{ortho.detail}。辨識與問答仍可使用。
        </div>
      )}

      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-xl font-bold">知識庫中的圖紙</h2>
          {parts.data && (
            <span className="text-xs text-ink-faint">
              {parts.data.items.length} 張 · 版本 {parts.data.kb_version}
            </span>
          )}
        </div>
        {parts.isLoading && <Loading />}
        {parts.error && <ApiErrorMessage error={parts.error} />}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {parts.data?.items.map((p) => <PartCard key={p.id} part={p} to={`/drawings/${p.id}`} />)}
        </div>
        {!!parts.data?.hidden && (
          <p className="mt-2 rounded-lg bg-paper-deep px-3 py-2 text-xs text-ink-soft">
            另有 {parts.data.hidden} 張圖紙不在目前身分的資料範圍（機密等級），沒有列出。
          </p>
        )}
        <p className="mt-3 text-xs text-ink-faint">
          示範資料為虛構工廠「示範精密機械」。新增圖紙只要加一個 JSON 與一支 CadQuery 標準模型，執行{" "}
          <code className="font-mono">make drawings index</code>，不改任何程式。也可以{" "}
          <Link to="/reconstruct" className="text-steel underline">
            上傳任何三視圖直接做 3D 重建
          </Link>
          。
        </p>
      </section>
    </div>
  );
}
