import { useQueryClient } from "@tanstack/react-query";
import { accountSwitch } from "../api/hooks";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Domain, View } from "./design";
import { canRerun, interruptForAccount, load, redactForeign, save, serialize } from "./persist";
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
  remove: (convId: string) => void;
  setActive: (convId: string, domain: Domain, key: string) => void;
  enterModule: (convId: string, domain: Domain, key?: string) => void;
  notice: (convId: string, text: string) => void;
  /** 身分換了：其他身分的非公開內容收起來 */
  onAccountChanged: (accountId: string, label: string, currentConvId: string | null) => void;
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

  // 存檔：串流中每個字都會更新狀態，等 400 ms 沒有變化再寫
  useEffect(() => {
    const t = window.setTimeout(() => save(serialize(convs, { collapsed, split, theme })), 400);
    return () => window.clearTimeout(t);
  }, [convs, collapsed, split, theme]);

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

  /** 這一次執行專用的 update：世代失效後的呼叫一律丟掉 */
  const guarded = useCallback(
    (convId: string, turnId: string, key: string) => {
      const g = ++generation.current;
      runs.current.set(key, g);
      const update = updateTurn(convId, turnId);
      return (f: (t: Turn) => Turn) => {
        if (runs.current.get(key) === g) update(f);
      };
    },
    [updateTurn],
  );

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
      void runTurn(turn, guarded(convId, turn.id, turn.id), { signal: ctrl.signal, token }).finally(() => {
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
      if (busyOf(id)) return id;
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
      if (busyOf(convId)) return;
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
    for (const t of c?.turns ?? []) invalidate(t.id);
    setConvs((cs) => cs.filter((x) => x.id !== convId));
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

  const onAccountChanged = useCallback((accountId: string, label: string, currentConvId: string | null) => {
    // 還在跑的一輪與任務用的是舊身分的 JWT：先中止並讓 callback 失效（之後再有事件也不會回填），再收起已經收到的內容
    const live = new Set<string>();
    const oldJwt = (t: Turn) =>
      t.route ? t.route.account.id !== accountId : !accountSwitch.startedAt || t.ts <= accountSwitch.startedAt;
    for (const c of convsRef.current)
      for (const t of c.turns)
        if ((RUNNING.has(t.phase) || taskRunning(t)) && oldJwt(t)) {
          invalidate(t.id);
          live.add(t.id);
        }
    setConvs((cs) => {
      const next = cs.map((c) => {
        // 每一段對話裡其他身分查到的非公開內容都收起來；提示只留在正在看的那一段
        const turns = c.turns.map((t) => (live.has(t.id) ? interruptForAccount(t) : redactForeign(t, accountId)));
        const stopped = c.turns.some((t) => live.has(t.id));
        const changed = turns.some((t, i) => t !== c.turns[i]);
        const text = `已切換身分為〈${label}〉：之後每一句都改用這張 JWT 判斷權限${stopped ? "；進行中的請求已中止" : ""}${changed ? "；其他身分查到的內部資料已收起" : ""}`;
        const notices = c.id === currentConvId && c.turns.length ? [...c.notices, { id: uid(), ts: Date.now(), text }] : c.notices;
        return { ...c, turns, notices };
      });
      convsRef.current = next;
      return next;
    });
  }, []);

  const task = (convId: string, turnId: string) => {
    const c = convsRef.current.find((x) => x.id === convId);
    return c?.turns.find((x) => x.id === turnId);
  };

  const startReconstruct = useCallback(
    (convId: string, turnId: string) => {
      const t = task(convId, turnId);
      if (t?.part?.kind !== "reconstruct") return;
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
      if (t?.part?.kind !== "schedule") return;
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
      if (t?.part?.kind !== "change") return;
      void runCommit(t.part as ChangePart, guarded(convId, turnId, `${turnId}:task`), note).finally(() => STALE_AFTER_WRITE.forEach((key) => void qc.invalidateQueries({ queryKey: [key] })));
    },
    [guarded, qc],
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
    onAccountChanged,
    startReconstruct,
    startSchedule,
    cancelTask,
    commitChange,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
