import { useSyncExternalStore } from "react";
import { ApiError } from "./client";

/**
 * 送出中的寫入（確認寫入、送主管核准、核准／退回、開立／取消工單、照片建檔入庫）。
 *
 * 寫入送出後，前端中止接收並不會撤銷伺服器上的交易：後端可能已經寫好異動單、工單或待核准單。所以
 * - 有寫入還沒回來時不能主動切換身分（useSwitchAccount 會拒絕），等結果回來再切換；
 * - 憑證更新、另一個分頁換了身分這類擋不住的情況，還沒回來的寫入標成「身分改變時送出中」（orphanWrites），
 *   回應到了只記結果（伺服器回覆完成／失敗／結果未確認），不顯示內容；
 * - **結果不能確定**（連不上、閘道逾時、5xx，或送出中頁面就關掉了）由這裡負責到底，不交給呼叫端：
 *   這一筆留在瀏覽器（重新載入、其他分頁也看得到），頁首提示「可能已經完成，請到紀錄核對」，
 *   而且同一件事（scope，例如「這個零件開工單」）在使用者按「已核對」之前不能再送——直接重送可能是第二筆。
 *   功能頁不必各自實作這套狀態：再送時 trackWrite 直接拒絕（不呼叫 API），呼叫端照常顯示錯誤；
 *   要停用按鈕可以用 useWriteBlocked(scope)。
 *
 * 留在瀏覽器的只有動作名稱、scope（零件、工單、核准單、草稿的編號）、狀態與時間，不存表單內容、JWT 或交接票。
 */
export type WriteStatus = "pending" | "done" | "failed" | "unknown";

export interface WriteEntry {
  id: string;
  label: string;
  /** 同一件事的範圍：同一個 scope 還有送出中／結果未確認的寫入時，不能再送 */
  scope?: string;
  status: WriteStatus;
  /** 送出時的身分已經失效（切換、憑證更新）：結果不接回原畫面，只在提示裡說明 */
  orphaned: boolean;
  /** 不是這個頁面送出的（另一個分頁，或重新載入前送出、還沒確認的） */
  foreign: boolean;
  ts: number;
}

const PREFIX = "artrag-writes-v1:";
/** 這一次載入頁面的代號：重新載入後，之前送出、還沒確認的都算「不是這個頁面的」 */
const newTab = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let TAB = newTab();

let own: WriteEntry[] = [];
let foreign: WriteEntry[] = [];
let all: WriteEntry[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => {
  all = [...own, ...foreign];
  listeners.forEach((l) => l());
};
const setOwn = (next: WriteEntry[]) => {
  own = next;
  emit();
};

type Stored = { label: string; scope?: string; status: "pending" | "unknown"; ts: number; tab: string };

function persist(id: string, rec: Stored): boolean {
  try {
    localStorage.setItem(PREFIX + id, JSON.stringify(rec));
    return true;
  } catch {
    return false;
  }
}
function unpersist(id: string) {
  try {
    localStorage.removeItem(PREFIX + id);
  } catch {
    /* 移除失敗：提示留著，保守 */
  }
}

/** 其他分頁、或重新載入前留下的：送出中或結果未確認，一律當成結果未確認 */
function readForeign(): WriteEntry[] {
  let keys: string[];
  try {
    keys = Object.keys(localStorage).filter((k) => k.startsWith(PREFIX));
  } catch {
    return [];
  }
  const out: WriteEntry[] = [];
  for (const k of keys)
    try {
      const raw = localStorage.getItem(k);
      if (raw === null) continue;
      const r = JSON.parse(raw) as Partial<Stored>;
      if (r.tab === TAB) continue;
      out.push({ id: k.slice(PREFIX.length), label: String(r.label ?? "寫入"), scope: r.scope, status: "unknown", orphaned: false, foreign: true, ts: Number(r.ts) || 0 });
    } catch {
      // 值壞掉：一樣當成結果未確認（保守），沒有 scope
      out.push({ id: k.slice(PREFIX.length), label: "寫入", status: "unknown", orphaned: false, foreign: true, ts: 0 });
    }
  return out;
}

function refreshForeign() {
  foreign = readForeign();
  emit();
}

if (typeof window !== "undefined") {
  refreshForeign();
  window.addEventListener("storage", (e) => {
    if (e.key === null || e.key.startsWith(PREFIX)) refreshForeign();
  });
}

/**
 * 寫入的結果能不能確定沒有落地：只有伺服器明確拒絕（4xx，例如權限不足、試算過期、驗證沒過）才算確定沒寫。
 * 連不上、閘道逾時、5xx 都算「結果不能確定」——後端是先 commit 交易、再重建庫存／讀回結果／寫稽核，
 * 後面這幾步出錯一樣回 500，交易不會回滾（change_service、production_repo）
 */
export const resultUnknown = (e: unknown) => {
  const status = (e as { status?: number } | null)?.status;
  return status === undefined || status === 0 || status >= 500;
};

/** 同一件事還有送出中、或結果未確認的寫入（這個頁面、其他分頁、重新載入前的都算） */
export const blockedBy = (scope: string) => all.find((e) => e.scope === scope && (e.status === "pending" || e.status === "unknown"));

/**
 * 送出一筆寫入。send 是真正呼叫 API 的函式：被擋下（同一件事還沒確認）或記不下「送出中」時不會呼叫它，
 * 直接以 4xx 的 ApiError 拒絕（確定沒有送出）。
 */
export function trackWrite<T>(label: string, send: () => Promise<T>, scope?: string): Promise<T> {
  const prior = scope ? blockedBy(scope) : undefined;
  if (prior)
    return Promise.reject(
      new ApiError(
        "WRITE_UNCONFIRMED",
        `上一次〈${prior.label}〉${prior.status === "pending" ? "還在送出中" : "送出後結果未確認，可能已經完成"}；請先到核准紀錄或庫存・工單核對，核對後在頁首按「已核對」再送`,
        "",
        409,
      ),
    );
  const id = `${TAB}-${++seq}`;
  const ts = Date.now();
  // 先記下「送出中」再送：送出中頁面關掉，重新載入後一樣知道這筆可能已經寫了
  if (!persist(id, { label, scope, status: "pending", ts, tab: TAB }))
    return Promise.reject(new ApiError("WRITE_NOT_RECORDED", "這台瀏覽器記不下「已送出」的狀態（儲存空間不足或被停用），為避免重複寫入，這次沒有送出", "", 400));
  setOwn([...own, { id, label, scope, status: "pending", orphaned: false, foreign: false, ts }]);
  const settle = (status: WriteStatus) => {
    const e = own.find((x) => x.id === id);
    if (status === "unknown") {
      // 結果不能確定：留在瀏覽器與提示裡，直到使用者核對後按「已核對」
      persist(id, { label, scope, status: "unknown", ts, tab: TAB });
      setOwn(own.map((x) => (x.id === id ? { ...x, status } : x)));
      return;
    }
    unpersist(id);
    if (!e) return;
    // 完成或確定沒寫：原畫面還在、身分沒變就由呼叫端顯示，這裡不留；身分改變了才留在提示裡說明
    setOwn(e.orphaned ? own.map((x) => (x.id === id ? { ...x, status } : x)) : own.filter((x) => x.id !== id));
  };
  let p: Promise<T>;
  try {
    p = send();
  } catch (err) {
    p = Promise.reject(err);
  }
  return p.then(
    (v) => {
      settle("done");
      return v;
    },
    (err: unknown) => {
      settle(resultUnknown(err) ? "unknown" : "failed");
      throw err;
    },
  );
}

/** 這個頁面還有寫入在等回應（切換身分前要等它） */
export const writesInFlight = () => own.some((e) => e.status === "pending");

/** 身分改變（切換、憑證更新、另一個分頁換了身分）：還沒回來的寫入改成「身分改變時送出中」，留下提示 */
export function orphanWrites() {
  if (own.some((e) => e.status === "pending" && !e.orphaned)) setOwn(own.map((e) => (e.status === "pending" ? { ...e, orphaned: true } : e)));
}

/** 使用者看過「身分改變時送出中」的提示：已經有確定結果的拿掉（還在等的、結果未確認的留著） */
export function dismissOrphans() {
  setOwn(own.filter((e) => !e.orphaned || e.status === "pending" || e.status === "unknown"));
}

/** 使用者已經到紀錄頁核對過這一筆：解除，同一件事可以再送 */
export function acknowledgeWrite(id: string) {
  unpersist(id);
  own = own.filter((e) => e.id !== id || e.status === "pending");
  foreign = foreign.filter((e) => e.id !== id);
  emit();
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};
const snapshot = () => all;

export const useWrites = () => useSyncExternalStore(subscribe, snapshot, snapshot);

/** 同一件事還有送出中、或結果未確認的寫入：按鈕停用、說明原因 */
export function useWriteBlocked(scope: string | null | undefined) {
  const list = useWrites();
  return scope ? list.find((e) => e.scope === scope && (e.status === "pending" || e.status === "unknown")) : undefined;
}

/** 測試用：清空（瀏覽器儲存由測試自己清） */
export function resetWrites() {
  own = [];
  foreign = [];
  emit();
}

/** 測試用：模擬重新載入（新的頁面代號、記憶體清空），從瀏覽器讀回留下的 */
export function simulateReloadForTest() {
  TAB = newTab();
  own = [];
  refreshForeign();
}
