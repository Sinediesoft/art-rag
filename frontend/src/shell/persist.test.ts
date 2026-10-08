import { describe, expect, it } from "vitest";
import { factoryRoute, route, sources, done } from "../test/transport";
import { deriveOutputs } from "./outputs";
import { archive, load, redactForeign, save, serialize, STORAGE_KEY } from "./persist";
import { overlayPipeline, buildStages } from "./stages";
import type { Conv, Turn } from "./types";

const base = (over: Partial<Turn>): Turn => ({
  id: "t",
  text: "原始輸入 0912-345-678",
  imageId: null,
  forced: null,
  at: "entry",
  ts: 1,
  account: { id: "planner", label: "生管" },
  phase: "done",
  route: null,
  failure: null,
  part: null,
  ...over,
});

const artTurn = (level = "公開") =>
  base({
    id: "art",
    route: route({ question: "我是[姓名1]，梵谷畫這幅畫的時候在哪裡？" }),
    part: { kind: "chat", target: { artwork_id: "met-436535" }, status: "done", sources: sources(level) as never, text: "1889 年在聖雷米 [1]。", done: done() as never, error: null },
  });

const factoryTurn = () =>
  base({
    id: "fac",
    route: factoryRoute(),
    part: { kind: "chat", target: { part_id: "mfg-002" }, status: "done", sources: sources("機密") as never, text: "公差 ±0.02 mm [1]。", done: done() as never, error: null },
  });

const sqlTurn = () =>
  base({
    id: "sql",
    route: route({ intent: "data_query", question: "法蘭還剩幾件可以出貨？", dispatch: { artwork_id: null, part_id: "mfg-002", part_label: "連接法蘭" } }),
    part: {
      kind: "sql",
      question: "法蘭還剩幾件可以出貨？",
      status: "done",
      attempts: [{ attempt: 1, sql: "SELECT secret_qty FROM stock", ok: true, error: null }],
      draft: "",
      result: { columns: ["倉庫", "可用"], rows: [["一廠成品倉", 12345]], row_count: 1, truncated: false, exec_ms: 2 },
      answer: "還有 12345 件。",
      done: null,
      error: null,
    },
  });

const conv = (turns: Turn[]): Conv => ({ id: "c1", createdAt: 1, updatedAt: 2, turns, module: "factory", active: { factory: "drawing:mfg-002" }, notices: [] });

describe("瀏覽器儲存：只存公開資料，不存憑證與內部資料", () => {
  it("不存 JWT、交接票、JWT payload、Jev 請求本文與代號對照、原始輸入", () => {
    const s = JSON.stringify(serialize([conv([artTurn(), factoryTurn(), sqlTurn()])], { collapsed: true, split: 62, theme: "light" }));
    expect(s).not.toContain("ticket-must-not-be-stored");
    expect(s).not.toContain("secret-unsigned");
    expect(s).not.toContain("claims");
    expect(s).not.toContain("route_ticket");
    expect(s).not.toContain("mapping");
    expect(s).not.toContain("masked_text");
    expect(s).not.toContain("0912-345-678");
    // 第 4 段剔除的段落：標題、主題都不留
    expect(s).not.toContain("觀眾留言");
    expect(s).not.toContain("洩密段落");
  });

  it("工廠內部資料（機密圖紙問答、Text-to-SQL）不存內容，只留編號與流程摘要", () => {
    const s = JSON.stringify(serialize([conv([factoryTurn(), sqlTurn()])], { collapsed: false, split: 50, theme: "dark" }));
    expect(s).not.toContain("±0.02");
    expect(s).not.toContain("12345");
    expect(s).not.toContain("secret_qty");
    const fac = archive(factoryTurn());
    expect(fac.redacted).toBe(true);
    expect(fac.refs).toEqual({ partId: "mfg-002" });
    expect(fac.answer).toBeUndefined();
    expect(fac.stages).toHaveLength(7);
    expect(Object.keys(fac.stages[0]).sort()).toEqual(["key", "short", "state"]);
    expect(archive(sqlTurn()).refs).toEqual({ sql: true });
  });

  it("公開畫作的回答與公開段落會保存；含非公開段落就不存", () => {
    expect(archive(artTurn()).answer).toBe("1889 年在聖雷米 [1]。");
    expect(archive(artTurn()).sources).toHaveLength(1);
    expect(archive(artTurn("內部")).redacted).toBe(true);
    expect(archive(artTurn("內部")).answer).toBeUndefined();
  });

  it("問句只存後端遮蔽個資後的版本；沒送達伺服器的一輪不存", () => {
    const s = serialize([conv([artTurn(), base({ id: "x", phase: "stopped" })])], { collapsed: false, split: 50, theme: "dark" });
    expect(s.convs[0].turns.map((t) => t.text)).toEqual(["我是[姓名1]，梵谷畫這幅畫的時候在哪裡？"]);
  });

  it("被擋下的請求只存流程摘要", () => {
    const blocked = base({
      route: route({ outcome: "blocked_guard", blocked: { stage: 2, rule: "Prompt 注入", log_no: "SEC-0002", judge: "地端", reason: "注入", degraded: false } }),
      part: { kind: "route" },
    });
    const a = archive(blocked);
    expect(a.summary).toContain("SEC-0002");
    expect(a.answer).toBeUndefined();
    expect(a.redacted).toBe(false);
  });

  it("存檔與讀回：紀錄、模組、正在看的成果、側欄收合、分隔線、主題", () => {
    save(serialize([conv([artTurn(), factoryTurn()])], { collapsed: true, split: 62, theme: "light" }));
    const { convs, prefs } = load();
    expect(prefs).toEqual({ collapsed: true, split: 62, theme: "light" });
    expect(convs[0].module).toBe("factory");
    expect(convs[0].active).toEqual({ factory: "drawing:mfg-002" });
    expect(convs[0].turns.every((t) => t.archived && !t.route && !t.part)).toBe(true);
    // 重新整理後：工廠成果照編號還原（展示區用目前的 JWT 重新讀取）
    const outs = deriveOutputs(convs[0]).list;
    expect(outs.map((o) => o.key)).toEqual(["artwork:met-436535", "timeline:met-436535", "drawing:mfg-002"]);
    expect(outs.find((o) => o.kind === "drawing")!.title).not.toContain("連接法蘭");
  });

  it("分隔線讀回時限制在 30%–70%；壞掉的紀錄當作沒有", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: 1, convs: [], collapsed: false, split: 95, theme: "dark" }));
    expect(load().prefs.split).toBe(70);
    localStorage.setItem(STORAGE_KEY, "{壞掉");
    expect(load().convs).toEqual([]);
  });

  it("切換身分：其他身分的非公開內容從畫面收起，公開內容留著", () => {
    const fac = redactForeign(factoryTurn(), "guest");
    expect(fac.part).toBeNull();
    expect(fac.route).toBeNull();
    expect(fac.archived?.redacted).toBe(true);
    expect(redactForeign(artTurn(), "guest").part).not.toBeNull();
    expect(redactForeign(factoryTurn(), "planner").part).not.toBeNull();
  });
});

describe("七段軌跡：以後端 pipeline 為準", () => {
  it("後端列為 block／skip 的段落照實顯示；完成時沒列到的段落是「沒有執行」", () => {
    const stages = buildStages(route(), {});
    const over = overlayPipeline(
      stages.map((s) => ({ ...s, state: "ok" })),
      [
        { stage: 3, name: "", status: "pass", by: "地端", detail: "" },
        { stage: 4, name: "", status: "block", by: "地端", detail: "洩密" },
      ],
      true,
    );
    expect(over.map((s) => s.state)).toEqual(["ok", "ok", "ok", "block", "skip", "skip", "skip"]);
  });
});
