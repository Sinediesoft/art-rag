import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** 來源：ArtRAG-前端demo/source/src/hooks.ts */
export function useMedia(q: string) {
  const [m, setM] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const h = () => setM(mq.matches);
    h();
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [q]);
  return m;
}

/** 黏在底部：內容長高時，如果使用者本來就在底部就跟著捲；換一段對話時直接到底 */
export function useStickyScroll(key: string | null, active: boolean) {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  /** 程式自己捲動的時間：那次捲動引發的 scroll 事件不算「使用者往上捲」 */
  const auto = useRef(0);
  const [atBottom, setAtBottom] = useState(true);

  useLayoutEffect(() => {
    const el = content.current;
    const sc = scroller.current;
    if (!sc) return;
    if (!el) {
      auto.current = 0;
      stick.current = false;
      sc.scrollTop = 0;
      return;
    }
    stick.current = true;
    auto.current = performance.now();
    sc.scrollTop = sc.scrollHeight;
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (!stick.current) return;
      auto.current = performance.now();
      sc.scrollTop = sc.scrollHeight;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [key, active]);

  const onScroll = () => {
    const sc = scroller.current!;
    const near = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 80;
    if (content.current && !near && performance.now() - auto.current < 250) {
      sc.scrollTop = sc.scrollHeight;
      return;
    }
    stick.current = near;
    setAtBottom(near);
  };
  const toBottom = () => {
    stick.current = true;
    auto.current = performance.now() + 600;
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: reducedMotion() ? "auto" : "smooth" });
  };
  const follow = () => {
    stick.current = true;
    requestAnimationFrame(toBottom);
  };
  return { scroller, content, onScroll, atBottom, toBottom, follow };
}

/** 抽屜、對話框開著時按 Esc 關閉 */
export function useEscape(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [open, close]);
}

export const reducedMotion =() => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
