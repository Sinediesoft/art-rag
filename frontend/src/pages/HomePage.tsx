import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useArtworks } from "../api/hooks";
import { ArtworkCard } from "../components/common/ArtworkCard";
import { ErrorMessage, Loading } from "../components/common/Feedback";
import { ImageUploader } from "../components/common/ImageUploader";
import { ApiError } from "../api/client";

const EXAMPLES = ["水邊草地上撐陽傘的人群", "金黃色麥田和高大的深色樹", "高聳的山峰與細長瀑布", "水面上的花與倒影"];

export function HomePage() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const artworks = useArtworks();

  const submit = (e?: FormEvent, text = q) => {
    e?.preventDefault();
    if (text.trim()) navigate(`/search?q=${encodeURIComponent(text.trim())}`);
  };

  return (
    <div className="flex flex-col gap-8">
      <section className="relative overflow-hidden rounded-2xl border border-line bg-card p-5 shadow-sm sm:p-8">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-10 -top-10 h-48 w-48 rounded-full bg-seal-soft blur-2xl"
        />
        <p className="text-sm font-bold tracking-widest text-seal">多模態 RAG 畫作導覽</p>
        <h1 className="mt-1 font-serif text-3xl font-black leading-tight sm:text-4xl">
          拍下一幅畫，
          <br className="sm:hidden" />
          聽它說自己的故事
        </h1>
        <p className="mt-2 max-w-xl text-ink-soft">
          系統先辨識是哪一幅畫，再從畫作知識庫檢索資料，以繁體中文回答，每一句都標註出處。
          拍到的是工廠圖紙也沒關係，會自動判斷並改用圖紙辨識。
        </p>
        <div className="mt-5 max-w-md">
          <ImageUploader onUploaded={(id) => navigate(`/search?image=${id}`)} />
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
            <Link
              to="/photo-diff"
              className="text-sm font-bold text-ink-soft underline-offset-2 hover:text-seal hover:underline"
            >
              比對兩張照片（修復前後、真跡與複製品）→
            </Link>
            <Link
              to="/artworks/intake"
              className="text-sm font-bold text-ink-soft underline-offset-2 hover:text-seal hover:underline"
            >
              知識庫還沒有的畫？拍照建檔 →
            </Link>
          </div>
        </div>

        <form onSubmit={submit} className="mt-5 flex max-w-xl gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="或用文字描述畫面，例如「山谷裡的騾隊」"
            className="min-w-0 flex-1 rounded-xl border border-line bg-paper px-4 py-3 outline-none transition focus:border-seal focus:bg-card"
          />
          <button className="shrink-0 rounded-xl bg-ink px-4 py-3 font-bold text-paper transition hover:bg-ink/85">
            以文搜圖
          </button>
        </form>
        <div className="mt-3 flex flex-wrap gap-2">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => submit(undefined, ex)}
              className="rounded-full border border-line bg-paper px-3 py-1 text-sm text-ink-soft transition hover:border-seal hover:text-seal"
            >
              {ex}
            </button>
          ))}
        </div>
      </section>

      <Link
        to="/drawings"
        className="blueprint group flex items-center gap-4 rounded-2xl border border-steel/25 p-4 shadow-sm transition hover:border-steel/60 sm:p-5"
      >
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-xl bg-steel text-white shadow-sm">
          <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="1.8">
            <path d="M3 21V9l6-4v4l6-4v4l6-4v16zM7 17h2m4 0h2" strokeLinejoin="round" strokeLinecap="round" />
          </svg>
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-xs font-bold tracking-widest text-steel">同一套架構 · 換一個知識庫</span>
          <span className="block text-lg font-black">工廠機械加工圖：三視圖 → 3D 模型 → 製程問答</span>
          <span className="block text-sm text-ink-soft">
            Ortho2CAD 把圖紙轉成 CadQuery 程式碼與 3D 模型；機密圖紙全程在本機處理
          </span>
        </span>
        <span aria-hidden className="text-xl text-steel transition group-hover:translate-x-1">
          →
        </span>
      </Link>

      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="font-serif text-xl font-bold">知識庫中的畫作</h2>
          {artworks.data && (
            <span className="text-xs text-ink-faint">
              {artworks.data.items.length} 幅 · 版本 {artworks.data.kb_version}
            </span>
          )}
        </div>
        {artworks.isLoading && <Loading />}
        {artworks.error && (
          <ErrorMessage
            message={(artworks.error as Error).message}
            requestId={(artworks.error as ApiError).requestId}
          />
        )}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {artworks.data?.items.map((a) => (
            <ArtworkCard key={a.id} artwork={a} to={`/artworks/${a.id}`} />
          ))}
        </div>
        <p className="mt-3 text-xs text-ink-faint">
          新增畫作只要加一個 JSON 檔與一張圖、執行 <code className="font-mono">make index</code>，不改任何程式。
        </p>
      </section>
    </div>
  );
}
