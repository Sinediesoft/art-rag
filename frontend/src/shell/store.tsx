import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { RouteResponse } from "../api/client";
import { ACCOUNT_EVENTS, identityBound } from "../api/hooks";
import { orphanWrites } from "../api/writes";

/** client.ts 的 renewToken 重新取得憑證時發出 */
const TOKEN_RENEWED = "artrag:token-renewed";
import type { Domain, View } from "./design";
import { applyMarks, canRerun, clearUnconfirmed, hasMark, interruptForAccount, load, markUnconfirmed, otherMarks, readMarks, redactForeign, redactPrivate, save, serialize, UNCONFIRMED_PREFIX } from "./persist";
import { commitChange as runCommit, runTurn, startReconstruct as runReconstruct, startSchedule as runSchedule } from "./runner";
import type { ChangePart, Conv, ReconstructPart, Turn } from "./types";
import type { EntryTheme } from "./theme";

export const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

/** 寫入後要重新抓的資料：庫存、工單、排程、核准（和功能頁共用 React Query 快取） */
const STALE_AFTER_WRITE = ["inventory-overview", "part-inventory", "part-plan", "production-overview", "approvals", "accounts", "audit"];

export interface AskInput {
  text: string;
  imageId?: string | null;
  forced?: string | null;
  /** 竄改過的 JWT（示範第 1 段）；只用在這一次請求，不進狀態也不存檔 */
  token?: string;
}

export interface Shell {
  convs: Conv[];
  collapsed: boolean;
  split: number;
  theme: EntryTheme;
  setCollapsed: (v: boolean) => void;
  setSplit: (v: number) => void;
  setTheme: (v: EntryTheme) => void;
  /** 在某段對話（null＝開新的一段）問一句；回傳對話 id */
  ask: (convId: string | null, at: View, input: AskInput) => string;
  /** 這段對話有沒有正在跑的一輪（第 1～7 段或串流中） */
  busyOf: (convId: string | null) => boolean;
  stop: (convId: string) => void;
  regenerate: (convId: string) => void;
  /** 從紀錄還原、內容沒有保存的一輪：以目前身分重新查詢 */
  rerun: (convId: string, turnId: string) => void;
  /** 刪除對話；有寫入送出中（還沒收到結果）時不刪，回傳 false */
  remove: (convId: string) => boolean;
  setActive: (convId: string, domain: Domain, key: string) => void;
  enterModule: (convId: string, domain: Domain, key?: string) => void;
  notice: (convId: string, text: string) => void;
  /** 正在切換身分：切換完成前不接受新提問 */
  switching: boolean;
  /** 身分確認了沒有（剛打開頁面、憑證更新後要等 /auth/accounts 回來）：確認前不接受提問與任務 */
  identityReady: boolean;
  /**
   * 身分世代（開始切換、身分改變、憑證更新時加一）：功能頁外框以它當 key，世代一變整個功能頁卸載重建，
   * 各頁自己的 state（SQL 結果、串流、表單）不會留到下一個身分
   */
  identityEpoch: number;
  /**
   * 目前的身分（/auth/accounts 或切換回應）：和之前不同時，中止舊身分還在跑的請求、清掉舊身分的快取、
   * 收起其他身分的非公開內容
   */
  /**
   * source：這份身分資料本身（/auth/accounts 的回應物件）。憑證更新時記下當時快取裡的那一份，
   * 之後畫面還拿著它（React Query 還沒通知到）時不能拿來重新確認身分；重設後重抓的一定是新物件
   */
  setAccount: (accountId: string, label: string, source?: object) => void;
  /** 目前正在看的對話（切換身分的提示只留在這一段） */
  setCurrentConv: (convId: string | null) => void;
  startReconstruct: (convId: string, turnId: string) => void;
  startSchedule: (convId: string, turnId: string) => void;
  cancelTask: (convId: string, turnId: string) => void;
  commitChange: (convId: string, turnId: string, note: string | null) => void;
}

const Ctx = createContext<Shell | null>(null);

export function useShell() {
  const s = useContext(Ctx);
  if (!s) throw new Error("useShell 要放在 <ShellProvider> 裡");
  return s;
}

const RUNNING = new Set<Turn["phase"]>(["routing", "running"]);

/** 這一輪有沒有使用者按了才開始、還在跑的任務（3D 重建、排程、寫入） */
function taskRunning(t: Turn) {
  const p = t.part;
  if (p?.kind === "reconstruct" || p?.kind === "schedule") return !!p.job && !["done", "error", "stopped"].includes(p.job.status);
  return p?.kind === "change" && p.status === "committing";
}

export function ShellProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const initial = useMemo(load, []);
  const [convs, setConvs] = useState<Conv[]>(initial.convs);
  const [collapsed, setCollapsed] = useState(initial.prefs.collapsed);
  const [split, setSplit] = useState(initial.prefs.split);
  const [theme, setTheme] = useState<EntryTheme>(initial.prefs.theme);
  const convsRef = useRef(convs);
  convsRef.current = convs;
  /** 每一輪（以及它的 3D／排程任務）的 AbortController */
  const controllers = useRef(new Map<string, AbortController>());
  /**
   * 每一次執行的世代：callback 只在自己的世代還有效時才寫回狀態。
   * 切換身分、刪除對話時讓舊世代失效，舊 JWT 的串流就算還有事件進來也不會回填畫面。
   */
  const runs = useRef(new Map<string, number>());
  const generation = useRef(0);
  /**
   * 身分世代：開始切換身分、身分改變（含憑證過期改發訪客）時加一。
   * 每次執行記下開始時的世代，世代變了就不再寫回；/agent/route 回來的帳號和目前身分不同也不寫回
   */
  const identity = useRef<{ accountId: string | null; label: string | null; epoch: number; stale: unknown }>({ accountId: null, label: null, epoch: 0, stale: undefined });
  const [switching, setSwitching] = useState(false);
  const switchingRef = useRef(false);
  /** 身分確認了沒有（剛打開頁面、憑證更新之後都要等 /auth/accounts 回來）：確認前不接受提問與任務 */
  const [identityReady, setIdentityReady] = useState(false);
  const [identityEpoch, setIdentityEpoch] = useState(0);
  /** 身分世代加一：舊的 callback 失效、功能頁重建、還沒收到結果的寫入改成「身分改變時送出中」 */
  const bumpEpoch = () => {
    identity.current.epoch++;
    setIdentityEpoch(identity.current.epoch);
    orphanWrites();
  };
  /** 這次切換開始時中止了請求（提示裡要說） */
  const stoppedBySwitch = useRef(false);
  const currentConv = useRef<string | null>(null);

  // 存檔：串流中每個字都會更新狀態，等 400 ms 沒有變化再寫
  const prefsRef = useRef({ collapsed, split, theme });
  prefsRef.current = { collapsed, split, theme };
  useEffect(() => {
    const t = window.setTimeout(() => save(serialize(convs, { collapsed, split, theme })), 400);
    return () => window.clearTimeout(t);
  }, [convs, collapsed, split, theme]);
  // 離開頁面（重新整理、關分頁、切到背景）：還在等 400 ms 的那一次立刻寫
  useEffect(() => {
    const flush = () => void save(serialize(convsRef.current, prefsRef.current));
    window.addEventListener("pagehide", flush);
    return () => window.removeEventListener("pagehide", flush);
  }, []);
  // 另一個分頁送出了寫入：這個分頁手上的同一輪（從紀錄還原的）立刻改成結果未確認，不再給「重新查詢」
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      // key 是 null：另一個分頁清空了整個 localStorage
      if (e.key !== null && !e.key.startsWith(UNCONFIRMED_PREFIX)) return;
      const marks = readMarks();
      setConvs((cs) => {
        let changed = false;
        const next = cs.map((c) => {
          const turns = c.turns.map((t) => applyMarks(t, marks));
          if (turns.every((t, i) => t === c.turns[i])) return c;
          changed = true;
          return { ...c, turns };
        });
        if (!changed) return cs;
        convsRef.current = next;
        return next;
      });
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const updateConv = useCallback((id: string, f: (c: Conv) => Conv) => setConvs((cs) => cs.map((c) => (c.id === id ? f(c) : c))), []);
  const updateTurn = useCallback(
    (convId: string, turnId: string) => (f: (t: Turn) => Turn) =>
      setConvs((cs) => cs.map((c) => (c.id === convId ? { ...c, turns: c.turns.map((t) => (t.id === turnId ? f(t) : t)) } : c))),
    [],
  );

  const control = (key: string) => {
    controllers.current.get(key)?.abort();
    const ctrl = new AbortController();
    controllers.current.set(key, ctrl);
    return ctrl;
  };

  /** 這一次執行專用的 update：執行世代失效、或身分世代變了（切換身分）之後的呼叫一律丟掉 */
  const guarded = useCallback(
    (convId: string, turnId: string, key: string) => {
      const g = ++generation.current;
      const epoch = identity.current.epoch;
      runs.current.set(key, g);
      const update = updateTurn(convId, turnId);
      return (f: (t: Turn) => Turn) => {
        if (runs.current.get(key) === g && identity.current.epoch === epoch) update(f);
      };
    },
    [updateTurn],
  );

  /** /agent/route 回來的帳號必須是目前確認過的身分；身分還沒確認（剛打開頁面、憑證更新中）一律不採信 */
  const acceptAccount = (accountId: string) => identity.current.accountId !== null && identity.current.accountId === accountId;
  /** 現在能不能送出會用到 JWT 的提問與任務：身分確認了、也沒有在切換 */
  const canSend = () => identity.current.accountId !== null && !switchingRef.current;

  /** 中止並讓這一輪（與它的任務）的 callback 失效 */
  const invalidate = (turnId: string) => {
    for (const k of [turnId, `${turnId}:task`]) {
      controllers.current.get(k)?.abort();
      controllers.current.delete(k);
      runs.current.delete(k);
    }
  };

  const start = useCallback(
    (convId: string, turn: Turn, token?: string) => {
      const ctrl = control(turn.id);
      const update = guarded(convId, turn.id, turn.id);
      const onForeign = (route: RouteResponse) => {
        // 回應屬於另一個身分（請求送出時的 JWT 已經不是目前的身分）：不寫回內容，只留問句與「已中止」
        invalidate(turn.id);
        updateTurn(convId, turn.id)((t) => interruptForAccount({ ...t, route }));
      };
      void runTurn(turn, update, { signal: ctrl.signal, token, accept: acceptAccount, onForeign }).finally(() => {
        if (controllers.current.get(turn.id) === ctrl) controllers.current.delete(turn.id);
        updateConv(convId, (c) => ({ ...c, updatedAt: Date.now() }));
      });
    },
    [guarded, updateConv],
  );

  const busyOf = useCallback((convId: string | null) => {
    const c = convsRef.current.find((x) => x.id === convId);
    return !!c?.turns.some((t) => RUNNING.has(t.phase));
  }, []);

  const ask = useCallback(
    (convId: string | null, at: View, input: AskInput) => {
      const text = input.text.trim();
      const id = convId && convsRef.current.some((c) => c.id === convId) ? convId : uid();
      if (!text && !input.imageId) return id;
      // 切換身分途中不送：這時候帶的是哪一張 JWT 不確定
      if (busyOf(id) || !canSend()) return id;
      const turn: Turn = {
        id: uid(),
        text,
        imageId: input.imageId ?? null,
        forced: input.forced ?? null,
        at,
        ts: Date.now(),
        tamper: !!input.token,
        account: null,
        phase: "routing",
        route: null,
        failure: null,
        part: null,
      };
      setConvs((cs) => {
        const exists = cs.some((c) => c.id === id);
        const next = exists
          ? cs.map((c) => (c.id === id ? { ...c, turns: [...c.turns, turn], updatedAt: Date.now() } : c))
          : [{ id, createdAt: Date.now(), updatedAt: Date.now(), turns: [turn], active: {}, notices: [] }, ...cs];
        convsRef.current = next;
        return next;
      });
      start(id, turn, input.token);
      return id;
    },
    [busyOf, start],
  );

  const stop = useCallback((convId: string) => {
    const c = convsRef.current.find((x) => x.id === convId);
    for (const t of c?.turns ?? []) {
      controllers.current.get(t.id)?.abort();
      controllers.current.get(`${t.id}:task`)?.abort();
    }
  }, []);

  const rerun = useCallback(
    (convId: string, turnId: string) => {
      if (busyOf(convId) || !canSend()) return;
      const c = convsRef.current.find((x) => x.id === convId);
      const t = c?.turns.find((x) => x.id === turnId);
      if (!t || !canRerun(t)) return;
      // 重跑同一句（已遮蔽個資的問句）：後端依目前的憑證重新判斷第 1～7 段
      const fresh: Turn = { ...t, account: null, phase: "routing", route: null, failure: null, part: null, archived: undefined, tamper: false, ts: Date.now() };
      updateTurn(convId, turnId)(() => fresh);
      start(convId, fresh);
    },
    [busyOf, start, updateTurn],
  );

  const regenerate = useCallback(
    (convId: string) => {
      const c = convsRef.current.find((x) => x.id === convId);
      const last = c?.turns.at(-1);
      if (last) rerun(convId, last.id);
    },
    [rerun],
  );

  const remove = useCallback((convId: string) => {
    const c = convsRef.current.find((x) => x.id === convId);
    // 寫入送出後還沒收到結果：刪掉對話就看不到結果了，等結果回來再刪
    if (c?.turns.some((t) => t.part?.kind === "change" && t.part.status === "committing")) return false;
    for (const t of c?.turns ?? []) invalidate(t.id);
    setConvs((cs) => cs.filter((x) => x.id !== convId));
    return true;
  }, []);

  const setActive = useCallback((convId: string, domain: Domain, key: string) => updateConv(convId, (c) => (c.active[domain] === key ? c : { ...c, active: { ...c.active, [domain]: key } })), [updateConv]);

  const enterModule = useCallback(
    (convId: string, domain: Domain, key?: string) => updateConv(convId, (c) => ({ ...c, module: domain, active: key ? { ...c.active, [domain]: key } : c.active })),
    [updateConv],
  );

  const notice = useCallback(
    (convId: string, text: string) => updateConv(convId, (c) => ({ ...c, notices: [...c.notices, { id: uid(), ts: Date.now(), text }] })),
    [updateConv],
  );

  /** 所有還在跑的一輪與任務：中止、讓 callback 失效，已經收到的內容收起來。回傳有沒有中止任何東西 */
  const interruptAll = () => {
    const live = new Set<string>();
    for (const c of convsRef.current)
      for (const t of c.turns)
        if (RUNNING.has(t.phase) || taskRunning(t)) {
          invalidate(t.id);
          live.add(t.id);
        }
    if (!live.size) return false;
    setConvs((cs) => {
      const next = cs.map((c) => (c.turns.some((t) => live.has(t.id)) ? { ...c, turns: c.turns.map((t) => (live.has(t.id) ? interruptForAccount(t) : t)) } : c));
      convsRef.current = next;
      return next;
    });
    return true;
  };

  /**
   * 開始切換身分：這一刻起在飛的請求可能帶舊 JWT、也可能帶新 JWT，一律不採信——全部中止並收起，
   * 身分世代加一（之後舊的 callback 都不會寫回），切換完成前不接受新提問。
   */
  const beginSwitch = useCallback(() => {
    bumpEpoch();
    switchingRef.current = true;
    setSwitching(true);
    if (interruptAll()) stoppedBySwitch.current = true;
  }, []);

  const endSwitch = useCallback(() => {
    switchingRef.current = false;
    setSwitching(false);
  }, []);

  const setAccount = useCallback(
    (accountId: string, label: string, source?: object) => {
      // 憑證更新之前取得的身分資料（React Query 還沒通知到畫面的舊結果）不能拿來重新確認身分
      if (source !== undefined && source === identity.current.stale) return;
      // 切換中的鎖只由切換事件解除（switched／failed）：/auth/accounts 定期重抓回來的還是舊身分時不能提早解鎖
      const prev = identity.current.accountId;
      if (prev === accountId) return;
      identity.current.accountId = accountId;
      identity.current.label = label;
      // 身分確認或換了：剛打開頁面第一次確認、憑證更新後重新確認、主動切換、憑證過期改發訪客都一樣處理——
      // 這之前就在跑的一律中止（世代加一，之後舊的 callback 都不寫回）、和身分有關的快取清掉重抓
      // （看不到的停在 403，不會繼續顯示舊資料）、其他身分查到的非公開內容收起。不能假定確認前沒有任何請求
      bumpEpoch();
      const stopped = interruptAll() || stoppedBySwitch.current;
      stoppedBySwitch.current = false;
      void qc.resetQueries({ predicate: (q) => identityBound(q) && q.queryKey[0] !== "accounts" });
      const currentConvId = currentConv.current;
      setConvs((cs) => {
        const next = cs.map((c) => {
          const turns = c.turns.map((t) => redactForeign(t, accountId));
          if (prev === null) return turns.some((t, i) => t !== c.turns[i]) ? { ...c, turns } : c;
          // 換了身分才留提示，而且只留在正在看的那一段
          const changed = turns.some((t, i) => t !== c.turns[i]);
          const text = `已切換身分為〈${label}〉：之後每一句都改用這張 JWT 判斷權限${stopped ? "；進行中的請求已中止" : ""}${changed ? "；其他身分查到的內部資料已收起" : ""}`;
          const notices = c.id === currentConvId && c.turns.length ? [...c.notices, { id: uid(), ts: Date.now(), text }] : c.notices;
          return { ...c, turns, notices };
        });
        convsRef.current = next;
        return next;
      });
      setIdentityReady(true);
    },
    [qc],
  );

  /**
   * 憑證更新（client.ts 的 renewToken：沒有、過期、後端重啟）：這時候身分可能已經變了（例如過期改發訪客），
   * 在 /auth/accounts 重新確認之前視為「身分未確認」：中止在飛的請求、清掉快取、鎖住提問；確認後由 setAccount 解鎖
   */
  const identityLost = useCallback(() => {
    identity.current.stale = qc.getQueryData(["accounts"]);
    identity.current.accountId = null;
    identity.current.label = null;
    bumpEpoch();
    interruptAll();
    // 已經完成（或停止、出錯）的非公開成果也立刻收起：不等 /auth/accounts 重新確認（它可能一直失敗）
    setConvs((cs) => {
      const next = cs.map((c) => {
        const turns = c.turns.map(redactPrivate);
        return turns.some((t, i) => t !== c.turns[i]) ? { ...c, turns } : c;
      });
      convsRef.current = next;
      return next;
    });
    setIdentityReady(false);
    void qc.resetQueries({ predicate: identityBound });
  }, [qc]);

  // 切換身分的過程（api/hooks.ts 的 useSwitchAccount 發出）：切換回應一到就同步更新身分，
  // 「切換成〇〇再試一次」緊接著送出的提問才會用新身分、不被擋
  useEffect(() => {
    const onSwitching = () => beginSwitch();
    const onSwitched = (e: Event) => {
      const d = (e as CustomEvent<{ id: string; label: string }>).detail;
      setAccount(d.id, d.label);
      // 切到同一個身分也要解鎖；開始切換時中止的請求在提示裡說明
      if (stoppedBySwitch.current && identity.current.accountId === d.id) stoppedBySwitch.current = false;
      endSwitch();
    };
    const onFailed = () => endSwitch();
    const onRenewed = () => identityLost();
    window.addEventListener(ACCOUNT_EVENTS.switching, onSwitching);
    window.addEventListener(ACCOUNT_EVENTS.switched, onSwitched);
    window.addEventListener(ACCOUNT_EVENTS.failed, onFailed);
    window.addEventListener(TOKEN_RENEWED, onRenewed);
    return () => {
      window.removeEventListener(ACCOUNT_EVENTS.switching, onSwitching);
      window.removeEventListener(ACCOUNT_EVENTS.switched, onSwitched);
      window.removeEventListener(ACCOUNT_EVENTS.failed, onFailed);
      window.removeEventListener(TOKEN_RENEWED, onRenewed);
    };
  }, [beginSwitch, endSwitch, identityLost, setAccount]);

  /** 使用者按了才開始的任務：身分還沒確認、切換身分途中、或這一輪是另一個身分問的，都不執行 */
  const mayAct = (t: Turn) => canSend() && (!t.account || t.account.id === identity.current.accountId);

  const task = (convId: string, turnId: string) => {
    const c = convsRef.current.find((x) => x.id === convId);
    return c?.turns.find((x) => x.id === turnId);
  };

  const startReconstruct = useCallback(
    (convId: string, turnId: string) => {
      const t = task(convId, turnId);
      if (t?.part?.kind !== "reconstruct" || !mayAct(t)) return;
      const ctrl = control(`${turnId}:task`);
      const update = guarded(convId, turnId, `${turnId}:task`);
      void runReconstruct(t.part as ReconstructPart, update, ctrl.signal).finally(() => {
        void qc.invalidateQueries({ queryKey: ["part-reconstructions"] });
        updateConv(convId, (c) => ({ ...c, updatedAt: Date.now() }));
      });
    },
    [guarded, qc, updateConv],
  );

  const startSchedule = useCallback(
    (convId: string, turnId: string) => {
      const t = task(convId, turnId);
      if (t?.part?.kind !== "schedule" || !mayAct(t)) return;
      const ctrl = control(`${turnId}:task`);
      void runSchedule(guarded(convId, turnId, `${turnId}:task`), ctrl.signal).finally(() => {
        void qc.invalidateQueries({ queryKey: ["production-overview"] });
        updateConv(convId, (c) => ({ ...c, updatedAt: Date.now() }));
      });
    },
    [guarded, qc, updateConv],
  );

  const cancelTask = useCallback(
    (convId: string, turnId: string) => {
      controllers.current.get(`${turnId}:task`)?.abort();
      updateTurn(convId, turnId)((t) => (t.part && (t.part.kind === "reconstruct" || t.part.kind === "schedule") && !t.part.job ? { ...t, part: { ...t.part, cancelled: true } } : t));
    },
    [updateTurn],
  );

  const commitChange = useCallback(
    (convId: string, turnId: string, note: string | null) => {
      const t = task(convId, turnId);
      if (t?.part?.kind !== "change" || !mayAct(t)) return;
      const part = t.part as ChangePart;
      if (part.status === "committing" || part.status === "unconfirmed" || !part.preview?.pending_id) return;
      // 這一輪已經有「已送出」標記（另一個分頁對同一輪重新試算後先送出了、結果還沒確認）：這份試算不能再送，
      // 收起成結果未確認——再送就是第二筆，而且會讓第一筆的標記被覆寫或清掉
      if (hasMark(turnId)) {
        updateTurn(convId, turnId)((x) => applyMarks(x));
        return;
      }
      // 送出之前先同步寫進瀏覽器：這一輪標成送出中（存檔版是「結果未確認」、不能重跑）。
      // 不等一般的 400 ms 延遲存檔——在那之前重新整理，會讀回試算完成、可以重跑的舊紀錄，再確認一次就是第二筆
      const action = note === null ? "commit" : "approval";
      const marked: Turn = { ...t, part: { ...part, status: "committing", action, error: null } };
      const next = convsRef.current.map((c) => (c.id === convId ? { ...c, turns: c.turns.map((x) => (x.id === turnId ? marked : x)) } : c));
      const failWith = (error: string) => updateTurn(convId, turnId)((x) => (x.part?.kind === "change" ? { ...x, part: { ...x.part, error } } : x));
      // 獨立的「已送出」標記（其他分頁的舊快照蓋不掉；每一次送出一個 key，誰也蓋不到誰）
      const mine = markUnconfirmed(turnId, convId, action);
      if (!mine) return failWith("這台瀏覽器記不下「已送出」的狀態（儲存空間不足或被停用），為避免重新整理後重複寫入，這次沒有送出");
      // 先寫、再看：寫完自己的標記後，同一輪如果還有別人的（另一個分頁和這裡幾乎同時按下、兩邊都先讀到沒有標記），
      // 這次就不送，撤回自己的標記。兩邊都「寫完才看」，所以不會兩邊都送出——
      // 最先寫入的那一邊若已經送出，後寫的一定看得到它；兩邊都看到對方就都不送，可以再按一次
      if (otherMarks(turnId, mine).length) {
        clearUnconfirmed([mine]);
        return failWith("另一個分頁同時送出了這一輪，為避免重複寫入，這次沒有送出；請先到核准紀錄或庫存核對");
      }
      if (!save(serialize(next, prefsRef.current))) {
        // 對話快照寫不進去也不送：重新整理後無法知道這筆已經送出過
        clearUnconfirmed([mine]);
        return failWith("這台瀏覽器記不下「已送出」的狀態（儲存空間不足或被停用），為避免重新整理後重複寫入，這次沒有送出");
      }
      convsRef.current = next;
      setConvs(next);
      const epoch = identity.current.epoch;
      void runCommit(part, guarded(convId, turnId, `${turnId}:task`), note)
        .then((result) => {
          // 結果確實回到這一輪（同一個身分、對話還在）：完成或伺服器明確拒絕，才解除「已送出」標記；
          // 結果不能確定、或回來時已經換了身分（結果沒有接回畫面），標記留著
          const alive = identity.current.epoch === epoch && convsRef.current.some((c) => c.id === convId);
          // 只解除自己這一次的標記：同一輪別的分頁送出的標記不受影響
          if (alive && (result === "done" || result === "failed")) clearUnconfirmed([mine]);
        })
        .finally(() => STALE_AFTER_WRITE.forEach((key) => void qc.invalidateQueries({ queryKey: [key] })));
    },
    [guarded, qc, updateTurn],
  );

  const value: Shell = {
    convs,
    collapsed,
    split,
    theme,
    setCollapsed,
    setSplit: (v) => setSplit(Math.round(Math.min(70, Math.max(30, v)))),
    setTheme,
    ask,
    busyOf,
    stop,
    regenerate,
    rerun,
    remove,
    setActive,
    enterModule,
    notice,
    switching,
    identityReady,
    identityEpoch,
    setAccount,
    setCurrentConv: (id) => void (currentConv.current = id),
    startReconstruct,
    startSchedule,
    cancelTask,
    commitChange,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
