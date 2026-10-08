import { useSyncExternalStore } from "react";

/**
 * 送出中的寫入（確認寫入、送主管核准、核准／退回、開立／取消工單、照片建檔入庫）。
 *
 * 寫入送出後，前端中止接收並不會撤銷伺服器上的交易：後端可能已經寫好異動單或待核准單。所以
 * - 有寫入還沒回來時不能主動切換身分（useSwitchAccount 會拒絕），等結果回來再切換；
 * - 憑證更新、另一個分頁換了身分這類擋不住的情況，還沒回來的寫入標成「身分改變時送出中」（orphanWrites），
 *   回應到了只記結果（伺服器回覆完成／失敗／連線中斷而結果未確認），不顯示內容；畫面提示到紀錄頁核對、不要直接重送。
 */
export type WriteStatus = "pending" | "done" | "failed" | "unknown";

export interface WriteEntry {
  id: number;
  label: string;
  status: WriteStatus;
  /** 送出時的身分已經失效（切換、憑證更新）：結果不接回原畫面，只在提示裡說明 */
  orphaned: boolean;
}

let entries: WriteEntry[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const set = (next: WriteEntry[]) => {
  entries = next;
  emit();
};

/**
 * 寫入的結果能不能確定沒有落地：只有伺服器明確拒絕（4xx，例如權限不足、試算過期、驗證沒過）才算確定沒寫。
 * 連不上、閘道逾時、5xx 都算「結果不能確定」——後端是先 commit 交易、再重建庫存／讀回結果／寫稽核，
 * 後面這幾步出錯一樣回 500，交易不會回滾（change_service、production_repo）
 */
export const resultUnknown = (e: unknown) => {
  const status = (e as { status?: number } | null)?.status;
  return status === undefined || status === 0 || status >= 500;
};

export function trackWrite<T>(label: string, p: Promise<T>): Promise<T> {
  const id = ++seq;
  set([...entries, { id, label, status: "pending", orphaned: false }]);
  const settle = (status: WriteStatus) => {
    const e = entries.find((x) => x.id === id);
    if (!e) return;
    // 原畫面還在、身分沒變：結果由呼叫端自己顯示，這裡不留
    set(e.orphaned ? entries.map((x) => (x.id === id ? { ...x, status } : x)) : entries.filter((x) => x.id !== id));
  };
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

export const writesInFlight = () => entries.some((e) => e.status === "pending");

/** 身分改變（切換、憑證更新、另一個分頁換了身分）：還沒回來的寫入改成「身分改變時送出中」，留下提示 */
export function orphanWrites() {
  if (entries.some((e) => e.status === "pending" && !e.orphaned)) set(entries.map((e) => (e.status === "pending" ? { ...e, orphaned: true } : e)));
}

/** 使用者看過提示：已經有結果的拿掉（還在等的留著） */
export function dismissOrphans() {
  set(entries.filter((e) => !e.orphaned || e.status === "pending"));
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};
const snapshot = () => entries;

export const useWrites = () => useSyncExternalStore(subscribe, snapshot, snapshot);

/** 測試用：清空 */
export function resetWrites() {
  set([]);
}
