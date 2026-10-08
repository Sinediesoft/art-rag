import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError } from "../../api/client";
import type { Copy } from "../../shell/design";
import { preprocessImage } from "../../lib/image";
import { Icon } from "./Icons";

/**
 * 來源：ArtRAG-前端demo/source/src/components/Composer.tsx。
 * 輸入框：文字＋附照片；Enter 送出、Shift+Enter 換行，注音／拼音選字（IME 組字中）時 Enter 不送出。
 * 照片選了就先上傳（POST /images，在本機辨識、不送 Jev），預覽用伺服器的網址，不留本機檔案。
 */
export function Composer({
  busy,
  onSend,
  onStop,
  copy,
  autoFocus,
  locked,
}: {
  busy: boolean;
  onSend: (text: string, imageId: string | null) => void;
  onStop: () => void;
  copy: Copy;
  autoFocus?: boolean;
  /** 暫時不能送出的原因（例如切換身分中：這時候帶哪一張 JWT 不確定） */
  locked?: string;
}) {
  const [text, setText] = useState("");
  const [imageId, setImageId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  useEffect(() => {
    if (autoFocus && window.matchMedia("(pointer: fine)").matches) ta.current?.focus();
  }, [autoFocus]);

  const upload = async (file?: File) => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const { image_id } = await api.uploadImage(await preprocessImage(file));
      setImageId(image_id);
    } catch (e) {
      setError(e instanceof ApiError ? `照片上傳失敗：${e.message}（${e.code}）` : `照片上傳失敗：${(e as Error).message}`);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const canSend = !busy && !locked && !uploading && (text.trim().length > 0 || !!imageId);
  const send = () => {
    if (!canSend) return;
    onSend(text.trim(), imageId);
    setText("");
    setImageId(null);
    setError(null);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className={`composer${imageId ? " has-photo" : ""}${busy ? " is-busy" : ""}`}>
      <div className="composer__box">
        {imageId && (
          <div className="composer__photo">
            <img src={api.uploadedImageUrl(imageId)} alt="附加的照片" />
            <button type="button" className="composer__unphoto" onClick={() => setImageId(null)} title="移除照片" aria-label="移除照片">
              <Icon name="x" strokeWidth={2.6} />
            </button>
            <span>在本機辨識・不送 Jev</span>
          </div>
        )}
        <textarea
          ref={ta}
          rows={1}
          value={text}
          maxLength={300}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={imageId ? copy.placeholderPhoto : copy.placeholder}
          className="composer__input"
          aria-label="輸入問題"
        />
        <div className="composer__bar">
          <button
            type="button"
            className="composer__attach"
            disabled={uploading}
            onClick={() => fileRef.current?.click()}
            title="附加照片（畫作或工廠圖紙，在本機辨識）"
            aria-label={copy.attachLabel}
          >
            {uploading ? <span className="spinner" aria-hidden /> : <Icon name="plus" strokeWidth={2} />}
            <span className="composer__attach-label">{uploading ? "上傳中…" : copy.attachLabel}</span>
          </button>
          <span className="composer__hint">Enter 送出・Shift+Enter 換行</span>
          {busy ? (
            <button type="button" className="composer__send is-stop" onClick={onStop} title="停止" aria-label="停止">
              <Icon name="stop" strokeWidth={3} />
            </button>
          ) : (
            <button type="button" className="composer__send" onClick={send} disabled={!canSend} title="送出（Enter）" aria-label="送出">
              <Icon name="send" strokeWidth={2.2} />
              <span className="composer__send-label">{copy.sendLabel}</span>
            </button>
          )}
        </div>
      </div>
      {locked && (
        <p className="composer__lock" role="status">
          {locked}
        </p>
      )}
      {error && (
        <p className="composer__error" role="alert">
          {error}
        </p>
      )}
      <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => void upload(e.target.files?.[0])} data-testid="composer-file" />
    </div>
  );
}
