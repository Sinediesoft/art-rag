import { describe, expect, it } from "vitest";
import { artApi } from "./artworks";
import { cadApi } from "./cad";
import { api } from "./client";
import { commonApi } from "./common";
import { drawingsApi } from "./drawings";
import { inventoryApi } from "./inventory";
import { jobsApi } from "./jobs";
import { scheduleApi } from "./schedule";

// 全部原始碼（字串）：架構檢查用，不執行
const sources = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const moduleSource = (name: string) => sources[`./${name}.ts`];

describe("五項功能各有 API 模組，共用同一個 client.ts 入口", () => {
  it("每項功能的端點歸在自己的模組，沒有遺漏也沒有重複", () => {
    const groups = { common: commonApi, art: artApi, drawings: drawingsApi, inventory: inventoryApi, cad: cadApi, schedule: scheduleApi, jobs: jobsApi };
    const all = Object.values(groups).flatMap((g) => Object.keys(g));
    expect(new Set(all).size).toBe(all.length); // 同一個名字不會出現在兩個模組
    expect(Object.keys(api).sort()).toEqual([...all].sort()); // client.ts 的 api 就是它們的總和
    // 各項功能的代表性端點
    expect(Object.keys(artApi)).toEqual(expect.arrayContaining(["searchImage", "searchText", "listArtworks", "photoColors", "photoAlignment", "createRegionDraft"]));
    expect(Object.keys(drawingsApi)).toEqual(expect.arrayContaining(["listParts", "getPart", "searchDrawing", "searchParts"]));
    expect(Object.keys(inventoryApi)).toEqual(expect.arrayContaining(["inventoryOverview", "partInventory", "changePreview", "changeCommit", "approve"]));
    expect(Object.keys(cadApi)).toEqual(expect.arrayContaining(["createCadJob", "cadJob", "partReconstructions"]));
    expect(Object.keys(scheduleApi)).toEqual(expect.arrayContaining(["productionOverview", "createWorkOrder", "createScheduleJob", "stopSchedule", "scheduleRun"]));
    expect(Object.keys(jobsApi)).toEqual(expect.arrayContaining(["job", "jobs", "cancelJob"]));
  });

  it("模組只呼叫自己領域的路徑", () => {
    const paths = (src: string) => [...src.matchAll(/["`](\/[a-z][\w\-/.${}()?=&!]*)/g)].map((m) => m[1]);
    const allowed: Record<string, RegExp> = {
      artworks: /^\/(search\/(image|text)|artworks|images|region-drafts)/,
      drawings: /^\/(parts|search\/(drawing|parts))/,
      inventory: /^\/(inventory|changes|approvals|eval\/sql-runs)/,
      cad: /^\/(cad|parts\/\$\{encodeURIComponent\(id\)\}\/reconstructions|eval\/cad-runs)/,
      schedule: /^\/(production|schedule|admin\/production\/reset)/,
      jobs: /^\/jobs/,
    };
    for (const [name, rx] of Object.entries(allowed)) {
      const bad = paths(moduleSource(name)).filter((p) => !rx.test(p));
      expect(bad, `${name}.ts 呼叫了不屬於這項功能的路徑`).toEqual([]);
    }
  });
});

describe("業務頁面不直連內部服務", () => {
  // 鍵是相對這個檔案的路徑：./ 開頭的就是 src/api 本身
  const app = Object.entries(sources).filter(([p]) => !p.startsWith("./") && !p.includes(".test.") && !p.includes("/test/"));

  it("元件、頁面、hook、狀態管理都不直接 fetch：全部經過 src/api", () => {
    const offenders = app.filter(([, src]) => /\bfetch\s*\(/.test(src)).map(([p]) => p);
    expect(offenders).toEqual([]);
  });

  it("沒有任何模型、資料庫、Redis 或 solver 的位址／埠號", () => {
    const rx = /(localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})|:(11434|8081|8082|5432|6379)\b|redis:\/\/|postgres(ql)?:\/\//i;
    const offenders = app.filter(([, src]) => rx.test(src)).map(([p]) => p);
    expect(offenders).toEqual([]);
  });

  it("EventSource／XMLHttpRequest 也只出現在 api 層", () => {
    const offenders = app.filter(([, src]) => /new\s+(EventSource|XMLHttpRequest|WebSocket)\b/.test(src)).map(([p]) => p);
    expect(offenders).toEqual([]);
  });
});
