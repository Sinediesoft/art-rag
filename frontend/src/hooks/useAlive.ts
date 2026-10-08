import { useCallback, useEffect, useRef } from "react";

/**
 * 這個元件還掛著嗎。卸載之後（離開頁面、切換身分時功能頁以新的 key 重建），晚到的回應只能丟掉：
 * React state 寫不回已卸載的元件，但改網址（setParams／navigate）、寫共用的 React Query 快取、
 * 接著送下一個請求都還做得到，這些副作用要先問 alive()。
 * StrictMode 會模擬卸載再掛上，所以 effect 每次掛上都重設為 true。
 */
export function useAlive() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return useCallback(() => alive.current, []);
}
