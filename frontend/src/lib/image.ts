// 圖片上傳前處理集中在這裡（共用層 §八）：EXIF 轉正、長邊 1024 px、JPEG 品質 0.85。
// 重新繪製到 canvas 後輸出，原檔的 EXIF（含 GPS）不會被帶上。
export const MAX_UPLOAD_MB = 10;
const LONG_EDGE = 1024;
const ALLOWED = ["image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"];

export async function preprocessImage(file: File): Promise<Blob> {
  if (file.type && !ALLOWED.includes(file.type)) {
    throw new Error("只支援 JPEG、PNG、WebP 圖片");
  }
  // imageOrientation: "from-image" 會依 EXIF 轉正
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, LONG_EDGE / Math.max(bitmap.width, bitmap.height));
  const w = Math.round(bitmap.width * scale);
  const h = Math.round(bitmap.height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  canvas.getContext("2d")!.drawImage(bitmap, 0, 0, w, h);
  bitmap.close();
  const blob = await new Promise<Blob | null>((r) => canvas.toBlob(r, "image/jpeg", 0.85));
  if (!blob) throw new Error("圖片處理失敗");
  if (blob.size > MAX_UPLOAD_MB * 1024 * 1024) throw new Error(`圖片超過 ${MAX_UPLOAD_MB} MB`);
  return blob;
}
