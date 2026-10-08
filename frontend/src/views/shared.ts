import type { ThreadActions } from "../components/shell/Message";
import type { Domain } from "../shell/design";
import { deriveOutputs } from "../shell/outputs";
import { useShell } from "../shell/store";
import type { Conv } from "../shell/types";

/**
 * 示範第 1 段：把目前 JWT 的 payload 改成主管、clearance 2，簽章亂填（原 AssistantPage 的展示）。
 * unsigned 是後端回傳的「不含簽章」的 header.payload；真正的憑證在 HttpOnly cookie，這裡讀不到也不保存。
 */
export function forgeToken(unsigned: string) {
  const [head, body] = unsigned.split(".");
  const fromB64 = (x: string) =>
    new TextDecoder().decode(Uint8Array.from(atob(x.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (x.length % 4)) % 4)), (c) => c.charCodeAt(0)));
  const toB64 = (x: string) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(x)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  const claims = JSON.parse(fromB64(body));
  return `${head}.${toB64(JSON.stringify({ ...claims, roles: ["主管"], clearance: 2 }))}.forged-signature`;
}

/** 入口與模組共用的問答串動作 */
export function useThreadActions(conv: Conv | null, mode: "brief" | "full", ask: ThreadActions["ask"], domain?: Domain): ThreadActions {
  const shell = useShell();
  const id = conv?.id ?? "";
  const outputs = deriveOutputs(conv);
  const active = domain ? (conv?.active[domain] ?? outputs.list.filter((o) => o.domain === domain).at(-1)?.key ?? null) : null;
  return {
    mode,
    ask,
    regenerate: () => id && shell.regenerate(id),
    rerun: (turnId) => id && shell.rerun(id, turnId),
    startReconstruct: (turnId) => id && shell.startReconstruct(id, turnId),
    startSchedule: (turnId) => id && shell.startSchedule(id, turnId),
    cancelTask: (turnId) => id && shell.cancelTask(id, turnId),
    commitChange: (turnId, note) => id && shell.commitChange(id, turnId, note),
    outputsOf: (turnId) => {
      const keys = outputs.byTurn[turnId] ?? [];
      return outputs.list.filter((o) => keys.includes(o.key) && (!domain || o.domain === domain));
    },
    activeOutput: active,
    showOutput: (key) => {
      if (id && domain) shell.setActive(id, domain, key);
    },
  };
}
