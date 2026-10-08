import { Fragment, type ReactNode } from "react";
import type { Conv, Turn } from "../../shell/types";
import { AssistantMessage, ThreadContext, UserMessage, type ThreadActions } from "./Message";

/**
 * 來源：ArtRAG-前端demo/source/src/components/Thread.tsx。
 * 問答串：入口與模組共用，差別只在 brief／full 與回答下方放什麼（入口：跳轉按鈕；模組：切換到另一個模組）。
 */
export function Thread({ conv, actions, afterOf }: { conv: Conv; actions: ThreadActions; afterOf: (t: Turn) => ReactNode }) {
  const items = [
    ...conv.turns.map((t) => ({ kind: "turn" as const, ts: t.ts, t })),
    ...conv.notices.map((n) => ({ kind: "notice" as const, ts: n.ts, n })),
  ].sort((a, b) => a.ts - b.ts);
  const last = conv.turns.at(-1)?.id;
  let index = 0;
  return (
    <ThreadContext.Provider value={actions}>
      {items.map((it) =>
        it.kind === "notice" ? (
          <p key={it.n.id} className="notice fade-in">
            <span>{it.n.text}</span>
          </p>
        ) : (
          <Fragment key={it.t.id}>
            <UserMessage turn={it.t} index={++index} />
            <AssistantMessage turn={it.t} isLast={it.t.id === last} after={afterOf(it.t)} />
          </Fragment>
        ),
      )}
    </ThreadContext.Provider>
  );
}
