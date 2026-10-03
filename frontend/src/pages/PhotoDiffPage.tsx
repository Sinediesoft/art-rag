import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { PairDiff } from "../components/align/AlignmentCards";
import { ImageUploader } from "../components/common/ImageUploader";

const SLOTS = [
  { key: "a", title: "照片 A：原本", hint: "修復前、真跡，或先拍的那一張" },
  { key: "b", title: "照片 B：要比的", hint: "修復後、複製品，或後來拍的那一張" },
] as const;

/** 兩張照片互比（docs/adr/012）：同一幅畫在同樣光線下各拍一張，找出形狀、顏色不一樣的地方 */
export function PhotoDiffPage() {
  const [params, setParams] = useSearchParams();
  const ids = { a: params.get("a"), b: params.get("b") };
  const set = (key: "a" | "b", id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set(key, id);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="t-display">比對兩張照片</h1>
        <p className="text-sm text-ink-80">
          同一幅畫拍兩張（修復前後、真跡與複製品），系統把照片 B 對齊到照片 A，標出形狀或顏色不一樣的地方。
          兩張請在同樣的光線、差不多的距離下拍；反光、陰影不同的地方也會被標出來。照片只在本機處理，不會送出去。
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2">
        {SLOTS.map((s) => {
          const id = ids[s.key];
          return (
            <section key={s.key} className="card flex flex-col gap-3 p-5">
              <div>
                <h2 className="font-semibold">{s.title}</h2>
                <p className="text-xs text-ink-48">{s.hint}</p>
              </div>
              {id ? (
                <>
                  <img
                    src={api.uploadedImageUrl(id)}
                    alt={s.title}
                    className="max-h-64 w-full rounded-md border border-hairline object-contain"
                  />
                  <button
                    type="button"
                    onClick={() => set(s.key, null)}
                    className="self-start text-sm font-semibold text-ink-80 underline-offset-2 hover:text-ink hover:underline"
                  >
                    換一張
                  </button>
                </>
              ) : (
                <ImageUploader
                  onUploaded={(newId) => set(s.key, newId)}
                  labels={{ camera: "拍照", file: "上傳照片", busy: "上傳中…" }}
                />
              )}
            </section>
          );
        })}
      </div>

      {ids.a && ids.b ? (
        <PairDiff a={ids.a} b={ids.b} />
      ) : (
        <p className="text-sm text-ink-48">兩張都放好之後就會開始比對。</p>
      )}
    </div>
  );
}
