// 共用的 HTTP 底層：所有功能模組（artworks、drawings、inventory、cad、schedule、jobs…）都經過這裡——
// 憑證處理（HttpOnly cookie、401 自動重新取得）、統一的錯誤格式（code／message／request_id）、
// 網址前綴。業務頁面與元件不直接 fetch；SSE 解析在 sse.ts。
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public requestId: string,
    public status: number,
  ) {
    super(message);
  }
}

/**
 * 七段權限控管第 1 段（docs/adr/015）：所有 /api/v1 請求都要 JWT（HttpOnly cookie，瀏覽器自動帶）。
 * 沒有憑證、過期或舊金鑰簽發（後端重啟）時閘道回 401：向 /auth/accounts 重新取得憑證（預設是一般員工）
 * 再重送一次，並通知頁首「憑證已更新」。簽章不符（TOKEN_INVALID）不重送：那是被竄改的憑證。
 */
const RENEWABLE = new Set(["UNAUTHENTICATED", "TOKEN_EXPIRED", "TOKEN_STALE"]);
let renewing: Promise<void> | null = null;

export async function renewToken(reason: string) {
  renewing ??= fetch(`${API_BASE}/api/v1/auth/accounts`)
    .then(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: reason }));
    })
    .finally(() => {
      renewing = null;
    });
  return renewing;
}

/** 401 的回應：要不要重新取得憑證後重送 */
export async function renewIfExpired(res: Response): Promise<boolean> {
  if (res.status !== 401) return false;
  const body = await res.clone().json().catch(() => null);
  const code = body?.error?.code;
  if (!RENEWABLE.has(code)) return false;
  await renewToken(code);
  return true;
}

export async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/v1${path}`, init);
  } catch {
    throw new ApiError("NETWORK_ERROR", "連不上伺服器，請確認網路或後端是否啟動", "", 0);
  }
  if (!retried && !path.startsWith("/auth/") && (await renewIfExpired(res))) {
    return request<T>(path, init, true);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(
      body?.error?.code ?? `HTTP_${res.status}`,
      body?.error?.message ?? res.statusText,
      body?.error?.request_id ?? res.headers.get("X-Request-ID") ?? "",
      res.status,
    );
  }
  return res.json() as Promise<T>;
}

export const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** 後端回傳的圖片路徑（/api/v1/...）加上 API_BASE */
export const assetUrl = (path: string) => `${API_BASE}${path}`;

/** 讀取同源的二進位資料（色塊分布圖、報告內嵌圖）：和 request 同一套憑證處理，失敗丟 ApiError */
export async function fetchBlob(url: string): Promise<Blob> {
  let res: Response;
  try {
    res = await fetch(url);
  } catch {
    throw new ApiError("NETWORK_ERROR", "連不上伺服器，請確認網路或後端是否啟動", "", 0);
  }
  if (await renewIfExpired(res)) res = await fetch(url);
  if (!res.ok) throw new ApiError(`HTTP_${res.status}`, res.statusText, res.headers.get("X-Request-ID") ?? "", res.status);
  return res.blob();
}

/** 讀取文字產物（例如 3D 重建的 code.py，受保護的下載端點） */
export async function fetchText(url: string): Promise<string> {
  return (await fetchBlob(url)).text();
}

/** 產生 Idempotency-Key：同一個使用者動作的重送共用一個 key */
export function newIdempotencyKey(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  return "k-" + Math.random().toString(36).slice(2) + Date.now().toString(36) + Math.random().toString(36).slice(2);
}

export const idemHeaders = (key?: string): Record<string, string> => (key ? { "Idempotency-Key": key } : {});

const KEYS = "artrag-idem-v1:";
const memoryKeys = new Map<string, string>();

/**
 * 同一個使用者動作（scope）的重送共用同一個 Idempotency-Key：連按、網路重試、重新送出
 * 都帶同一個 key，伺服器只寫入一次。存在這個分頁的 sessionStorage（讀不到時退回記憶體）；
 * 動作確定完成或確定被拒絕後用 releaseKey 放掉，下一次同樣的動作才是新的一件事。
 */
export function stableKey(scope: string): string {
  try {
    const have = sessionStorage.getItem(KEYS + scope);
    if (have) return have;
    const key = newIdempotencyKey();
    sessionStorage.setItem(KEYS + scope, key);
    return key;
  } catch {
    if (!memoryKeys.has(scope)) memoryKeys.set(scope, newIdempotencyKey());
    return memoryKeys.get(scope)!;
  }
}

export function releaseKey(scope: string): void {
  memoryKeys.delete(scope);
  try {
    sessionStorage.removeItem(KEYS + scope);
  } catch {
    /* 沒有 sessionStorage 就只剩記憶體 */
  }
}

/** 清掉所有保存的 key（切換身分、登出）：不同身分的動作不共用 key */
export function clearKeys(): void {
  memoryKeys.clear();
  try {
    for (const k of Object.keys(sessionStorage)) if (k.startsWith(KEYS)) sessionStorage.removeItem(k);
  } catch {
    /* 同上 */
  }
}

/**
 * 帶 Idempotency-Key 執行一次寫入。結果確定（成功，或伺服器明確拒絕）就放掉 key；
 * 結果不確定（連不上、閘道逾時、5xx）留著，之後同一個動作重送拿到同一個 key。
 */
export async function withIdempotency<T>(scope: string, run: (key: string) => Promise<T>): Promise<T> {
  const key = stableKey(scope);
  try {
    const out = await run(key);
    releaseKey(scope);
    return out;
  } catch (e) {
    const status = e instanceof ApiError ? e.status : 0;
    if (status >= 400 && status < 500 && status !== 408 && status !== 429) releaseKey(scope);
    throw e;
  }
}
