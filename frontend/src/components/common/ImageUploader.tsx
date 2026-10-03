import { useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import { preprocessImage } from "../../lib/image";

/** 拍照／上傳：手機直接開後鏡頭；上傳前先在瀏覽器轉正、壓縮、去除 EXIF */
export function ImageUploader({
  onUploaded,
  tone = "seal",
  labels = { camera: "拍照辨識", file: "上傳照片" },
}: {
  onUploaded: (imageId: string) => void;
  tone?: "seal" | "steel";
  /** busy：處理中顯示的字（預設「辨識中…」） */
  labels?: { camera: string; file: string; busy?: string };
}) {
  const cameraRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handle = async (file?: File) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await preprocessImage(file);
      const { image_id } = await api.uploadImage(blob);
      onUploaded(image_id);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message}（${e.requestId}）` : (e as Error).message);
    } finally {
      setBusy(false);
      if (cameraRef.current) cameraRef.current.value = "";
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => cameraRef.current?.click()}
          className={`flex items-center justify-center gap-2 rounded-xl px-4 py-3.5 font-bold text-white shadow-sm transition disabled:opacity-60 ${
            tone === "steel" ? "bg-steel hover:bg-steel-deep" : "bg-seal hover:bg-seal-deep"
          }`}
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M4 8h3l2-3h6l2 3h3v11H4z" strokeLinejoin="round" />
            <circle cx="12" cy="13" r="3.5" />
          </svg>
          {busy ? (labels.busy ?? "辨識中…") : labels.camera}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
          className="flex items-center justify-center gap-2 rounded-xl border border-ink/15 bg-card px-4 py-3.5 font-bold text-ink transition hover:border-ink/40 disabled:opacity-60"
        >
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="4" y="4" width="16" height="16" rx="2" />
            <path d="m4 16 5-5 4 4 3-3 4 4" strokeLinejoin="round" />
          </svg>
          {labels.file}
        </button>
      </div>
      <input
        ref={cameraRef}
        type="file"
        accept="image/*"
        capture="environment"
        hidden
        onChange={(e) => handle(e.target.files?.[0])}
      />
      <input
        ref={fileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        hidden
        onChange={(e) => handle(e.target.files?.[0])}
      />
      {error && <p className="text-sm text-seal">{error}</p>}
    </div>
  );
}
