import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Output } from "../../shell/outputs";
import type { Conv, Part, Turn } from "../../shell/types";
import { arrange, DEFAULT_LAYOUT, panelOf, pendingWrites, presetOf, Workbench } from "./Workbench";

const conv = (parts: Part[] = []): Conv => ({
  id: "c1",
  createdAt: 0,
  updatedAt: 0,
  active: {},
  notices: [],
  turns: parts.map(
    (part, i): Turn => ({ id: `t${i}`, text: "", imageId: null, forced: null, at: "factory", ts: i, account: null, phase: "done", route: null, failure: null, part }),
  ),
});

const stock = (key: string, n: number): Output => ({
  key,
  kind: "stock",
  domain: "factory",
  turnId: key,
  title: `庫存查詢 ${n}`,
  meta: "唯讀查詢",
  sql: {
    kind: "sql",
    question: "還剩幾件",
    status: "done",
    attempts: [],
    draft: "",
    result: { columns: ["倉庫", "可用"], rows: [[`第 ${n} 次的倉庫`, n]], row_count: 1, truncated: false, exec_ms: 1 },
    answer: "",
    done: null,
    error: null,
  } as Output["sql"],
});

function renderBench(props: Partial<Parameters<typeof Workbench>[0]> = {}) {
  const onAsk = vi.fn();
  const onSelect = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const all = { conv: conv(), outputs: [], active: undefined, subject: null, narrow: false, locked: false, onAsk, onSelect, ...props };
  const r = render(
    <QueryClientProvider client={qc}>
      <Workbench {...all} />
    </QueryClientProvider>,
  );
  const rerender = (next: Partial<Parameters<typeof Workbench>[0]>) =>
    r.rerender(
      <QueryClientProvider client={qc}>
        <Workbench {...all} {...next} />
      </QueryClientProvider>,
    );
  return { onAsk, onSelect, rerender };
}

const panel = (id: string) => document.querySelector<HTMLElement>(`.wb-panel[data-panel="${id}"]`)!;
const hidden = (id: string) => panel(id).classList.contains("is-hidden");

beforeEach(() => localStorage.clear());

describe("工作台版面（arrange）", () => {
  it("四格：兩條縫；收合一塊變三塊（下面一塊占滿寬）；兩塊左右並排；聚焦：左大右一欄；最大化只剩一塊", () => {
    const four = arrange(DEFAULT_LAYOUT);
    expect(Object.keys(four.rects)).toEqual(["drawing", "model", "schedule", "stock"]);
    four.rects.stock!.forEach((v, i) => expect(v).toBeCloseTo([0.5, 0.54, 0.5, 0.46][i]));
    expect(four.gutters.map((g) => g.key)).toEqual(["col", "row"]);

    const three = arrange({ ...DEFAULT_LAYOUT, min: ["model"] });
    expect(three.rects.model).toBeUndefined();
    three.rects.stock!.forEach((v, i) => expect(v).toBeCloseTo([0, 0.54, 1, 0.46][i]));
    expect(three.gutters[0]).toMatchObject({ key: "col", a: 0, b: 0.54 });

    const two = arrange({ ...DEFAULT_LAYOUT, min: ["model", "stock"] });
    expect(two.rects).toEqual({ drawing: [0, 0, 0.5, 1], schedule: [0.5, 0, 0.5, 1] });

    const focus = arrange({ ...DEFAULT_LAYOUT, focus: "schedule" });
    expect(focus.rects.schedule).toEqual([0, 0, 0.64, 1]);
    expect(focus.rects.drawing?.[1]).toBe(0);
    expect(focus.rects.stock?.[1]).toBeCloseTo(2 / 3);
    expect(focus.gutters.map((g) => g.key)).toEqual(["fcol"]);

    const max = arrange({ ...DEFAULT_LAYOUT, focus: "schedule", max: "model" });
    expect(max.rects).toEqual({ model: [0, 0, 1, 1] });
    expect(max.gutters).toEqual([]);
    // 最大化的那塊被收合：照收合後的版面
    expect(Object.keys(arrange({ ...DEFAULT_LAYOUT, max: "model", min: ["model"] }).rects)).toHaveLength(3);
  });

  it("預設版面的辨識：四格、設計檢視（聚焦圖紙）、生產檢視（聚焦排程）；收合或最大化就都不是", () => {
    expect(presetOf(DEFAULT_LAYOUT)).toBe("grid");
    expect(presetOf({ ...DEFAULT_LAYOUT, focus: "drawing" })).toBe("design");
    expect(presetOf({ ...DEFAULT_LAYOUT, focus: "schedule" })).toBe("prod");
    expect(presetOf({ ...DEFAULT_LAYOUT, focus: "model" })).toBeNull();
    expect(presetOf({ ...DEFAULT_LAYOUT, min: ["stock"] })).toBeNull();
    expect(presetOf({ ...DEFAULT_LAYOUT, max: "drawing" })).toBeNull();
  });

  it("成果放進哪個板塊：查詢結果也放進庫存板塊", () => {
    expect(panelOf("drawing")).toBe("drawing");
    expect(panelOf("model")).toBe("model");
    expect(panelOf("schedule")).toBe("schedule");
    expect(panelOf("stock")).toBe("stock");
    expect(panelOf("query")).toBe("stock");
    expect(panelOf("artwork")).toBeNull();
  });
});

describe("工作台操作", () => {
  it("版面切換：設計檢視聚焦圖紙、生產檢視聚焦排程、四格還原；記在這個瀏覽器", () => {
    renderBench();
    const presets = screen.getByRole("radiogroup", { name: "版面" });
    expect(within(presets).getByRole("radio", { name: "四格" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(presets).getByRole("radio", { name: "生產檢視" }));
    expect(panel("schedule").style.left).toBe("calc(0% + 0px)");
    expect(panel("schedule").style.width).toBe("calc(64% - 5px)");
    expect(document.querySelector(".wb-head__sub")?.textContent).toContain("聚焦〈排程〉");
    expect(JSON.parse(localStorage.getItem("artrag-workbench")!).focus).toBe("schedule");
    fireEvent.click(within(presets).getByRole("radio", { name: "設計檢視" }));
    expect(panel("drawing").style.width).toBe("calc(64% - 5px)");
    fireEvent.click(within(presets).getByRole("radio", { name: "四格" }));
    expect(panel("drawing").style.width).toBe("calc(50% - 5px)");
  });

  it("收合到下方、從收合列展開；最大化（雙擊標題列也可以）只剩一塊，再按一次還原", () => {
    renderBench();
    fireEvent.click(screen.getByRole("button", { name: "收合庫存" }));
    expect(hidden("stock")).toBe(true);
    expect(panel("stock").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByRole("radiogroup", { name: "版面" }).querySelector("[aria-checked=true]")).toBeNull();
    fireEvent.click(screen.getByTitle("展開庫存"));
    expect(hidden("stock")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "最大化3D" }));
    expect(["drawing", "schedule", "stock"].every(hidden)).toBe(true);
    expect(panel("model").style.width).toBe("calc(100% - 0px)");
    fireEvent.click(screen.getByRole("button", { name: "還原3D" }));
    expect(hidden("drawing")).toBe(false);
    fireEvent.doubleClick(panel("drawing").querySelector(".wb-panel__head")!);
    expect(hidden("model")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "重設版面" }));
    expect(["drawing", "model", "schedule", "stock"].some(hidden)).toBe(false);
  });

  it("板塊之間的縫：方向鍵調整（限制在 22%–78%），雙擊回到中間", () => {
    renderBench();
    const col = screen.getByRole("separator", { name: /寬度/ });
    expect(col.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(col, { key: "ArrowRight" });
    expect(col.getAttribute("aria-valuenow")).toBe("52");
    expect(panel("drawing").style.width).toBe("calc(52% - 5px)");
    for (let i = 0; i < 30; i++) fireEvent.keyDown(col, { key: "ArrowRight" });
    expect(col.getAttribute("aria-valuenow")).toBe("78");
    fireEvent.doubleClick(col);
    expect(col.getAttribute("aria-valuenow")).toBe("50");
    const row = screen.getByRole("separator", { name: /高度/ });
    fireEvent.keyDown(row, { key: "ArrowUp" });
    expect(row.getAttribute("aria-valuenow")).toBe("52");
  });

  it("空板塊的建議問句送進左邊對話；不能送出時停用", () => {
    const { onAsk, rerender } = renderBench({ subject: { kind: "part", id: "mfg-002", label: "連接法蘭", meta: "" } });
    fireEvent.click(within(panel("model")).getByRole("button", { name: "把連接法蘭轉成 3D" }));
    expect(onAsk).toHaveBeenCalledWith("把連接法蘭轉成 3D");
    rerender({ locked: true });
    expect((within(panel("stock")).getByRole("button", { name: "連接法蘭還剩幾件可以出貨？" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("新成果放進對應的板塊：收合中的板塊自動拉回來並標成目前成果；板塊裡切換上一份、下一份", () => {
    const a = stock("sql:1", 1);
    const b = stock("sql:2", 2);
    const { onSelect, rerender } = renderBench({ outputs: [a], active: a });
    expect(panel("stock").classList.contains("is-active")).toBe(true);
    expect(panel("stock").textContent).toContain("第 1 次的倉庫");
    fireEvent.click(screen.getByRole("button", { name: "收合庫存" }));
    expect(hidden("stock")).toBe(true);
    rerender({ outputs: [a, b], active: b });
    expect(hidden("stock")).toBe(false);
    expect(panel("stock").textContent).toContain("第 2 次的倉庫");
    expect(panel("stock").querySelector(".wb-panel__nav")?.textContent).toContain("2/2");
    fireEvent.click(screen.getByRole("button", { name: "庫存：上一份" }));
    expect(onSelect).toHaveBeenCalledWith("sql:1");
    // 模組把它設成目前成果；在板塊裡自己切換的不動版面（收合的圖紙不會被拉回來）
    fireEvent.click(screen.getByRole("button", { name: "收合圖紙" }));
    rerender({ outputs: [a, b], active: a });
    expect(panel("stock").textContent).toContain("第 1 次的倉庫");
    expect(hidden("drawing")).toBe(true);
  });
});

describe("寫入進度（pendingWrites）：只顯示後端的狀態與編號", () => {
  const preview = (op: string, next: string) => ({ op, op_label: op === "stock_adjust" ? "庫存調整" : "工單改期", next, summary: "SYNTH_NUMBERS 18 → 23" });
  it("庫存異動：待確認 → 已寫入（提醒板塊裡的數字是寫入前查的）；工單異動放在排程板塊；試算數字不出現", () => {
    const ready = { kind: "change", status: "ready", preview: preview("stock_adjust", "confirm"), committed: null, approval: null, error: null } as unknown as Part;
    let w = pendingWrites(conv([ready]));
    expect(w.stock?.text).toContain("「庫存調整」待確認");
    expect(w.stock?.text).not.toContain("SYNTH_NUMBERS");
    const done = { ...ready, committed: { change_no: "CH-7", op: "stock_adjust" } } as unknown as Part;
    w = pendingWrites(conv([done]));
    expect(w.stock).toMatchObject({ tone: "done" });
    expect(w.stock?.text).toContain("CH-7");
    const approval = { kind: "change", status: "ready", preview: preview("wo_reschedule", "approval"), committed: null, approval: null, error: null } as unknown as Part;
    w = pendingWrites(conv([approval]));
    expect(w.schedule?.text).toContain("送主管核准");
    expect(w.stock).toBeUndefined();
    const sent = { ...approval, approval: { ap_no: "AP-3", op: "wo_reschedule", op_label: "工單改期" } } as unknown as Part;
    expect(pendingWrites(conv([sent])).schedule?.text).toContain("AP-3");
    const lost = { ...ready, status: "unconfirmed" } as unknown as Part;
    expect(pendingWrites(conv([lost])).stock).toMatchObject({ tone: "warn" });
  });

  it("重新排程：還沒按開始 → 求解中 → 完成（結果已是成果，不再提示）；取消就不提示", () => {
    const wait = { kind: "schedule", job: null, cancelled: false } as Part;
    expect(pendingWrites(conv([wait])).schedule?.text).toContain("開始排程");
    const solving = { kind: "schedule", job: { status: "solving" }, cancelled: false } as unknown as Part;
    expect(pendingWrites(conv([wait, solving])).schedule?.text).toContain("求解中");
    const done = { kind: "schedule", job: { status: "done" }, cancelled: false } as unknown as Part;
    expect(pendingWrites(conv([solving, done])).schedule).toBeUndefined();
    expect(pendingWrites(conv([{ ...wait, cancelled: true } as Part])).schedule).toBeUndefined();
  });
});
