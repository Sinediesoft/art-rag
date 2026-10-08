import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

/** 測試裡的視窗寬度（useMedia 依它回答 min-width 查詢）；各測試可以改 */
export const viewport = { width: 1440 };

function matches(query: string) {
  const min = query.match(/min-width:\s*(\d+)px/);
  const max = query.match(/max-width:\s*(\d+)px/);
  if (min) return viewport.width >= Number(min[1]);
  if (max) return viewport.width <= Number(max[1]);
  if (query.includes("pointer: fine")) return false;
  if (query.includes("prefers-reduced-motion")) return true; // 測試不跑轉場動畫
  return false;
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: matches(query),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }),
});

class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
// jsdom 沒有 ResizeObserver、捲動與 pointer capture
Object.assign(globalThis, { ResizeObserver: RO });
Element.prototype.scrollTo = function () {};
Element.prototype.scrollIntoView = function () {};
HTMLElement.prototype.setPointerCapture = function () {};
HTMLElement.prototype.releasePointerCapture = function () {};

beforeEach(() => {
  viewport.width = 1440;
  localStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
