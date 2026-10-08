import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import App from "./App";
import { STORAGE_KEY } from "./shell/persist";
import { ShellProvider } from "./shell/store";
import { viewport } from "./test/setup";
import { baseHandlers, done, factoryRoute, json, mockTransport, route, sources, sse } from "./test/transport";

let where = "/";
function Where() {
  where = useLocation().pathname;
  return null;
}

function renderApp(path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <ShellProvider>
          <App />
          <Where />
        </ShellProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 工廠：圖紙問答 → 庫存查詢；畫作：作品問答。所有回應都來自 mock transport */
function factoryBackend() {
  const t = mockTransport();
  baseHandlers(t);
  t.on("POST", "/agent/route", ({ body }) => {
    const q = (body as { question: string }).question;
    if (q.includes("幾件"))
      return json(route({ question: q, intent: "data_query", intent_label: "庫存・訂單・工單查詢", dispatch: { artwork_id: null, part_id: null, question: q } }));
    if (q.includes("梵谷")) return json(route({ question: q }));
    return json(factoryRoute({ question: q }));
  });
  t.on("POST", "/chat", ({ body }) =>
    (body as { part_id?: string }).part_id
      ? sse([["sources", sources("機密")], ["token", { text: "外徑公差 ±0.02 mm [1]。" }], ["done", done()]])
      : sse([["sources", sources()], ["token", { text: "1889 年在聖雷米 [1]。" }], ["done", done()]]),
  );
  t.on("POST", "/inventory/ask", () =>
    sse([
      ["sql", { attempt: 1, sql: "SELECT 1", ok: true, error: null }],
      ["result", { columns: ["倉庫", "可用"], rows: [["一廠成品倉", 12], ["二廠成品倉", 3]], row_count: 2, truncated: false, exec_ms: 2 }],
      ["token", { text: "還有 15 件。" }],
      ["done", { request_id: "r" }],
    ]),
  );
  t.on("GET", "/parts/mfg-002", () =>
    json({
      id: "mfg-002",
      part_no: "FLG-2002",
      drawing_no: "D-24-0203",
      revision: "C",
      name: { zh: "連接法蘭" },
      category: "法蘭",
      material: "S45C",
      density_g_cm3: 7.8,
      surface: "發黑",
      company: "",
      owner: "生管",
      confidentiality: "機密",
      geometry: {},
      descriptions: [],
      tags: [],
      drawing_url: "/api/v1/parts/mfg-002/drawing",
      thumb_url: "/api/v1/parts/mfg-002/drawing?size=thumb",
      model_url: null,
      step_url: null,
    }),
  );
  return t;
}

const ask = async (text: string) => {
  const input = screen.getAllByLabelText("輸入問題").at(-1)!;
  fireEvent.change(input, { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(input, { key: "Enter" });
  });
};

const idle = () => waitFor(() => expect(document.querySelectorAll('.msg--ai[data-phase="routing"], .msg--ai[data-phase="running"]').length).toBe(0));

describe("入口：對話紀錄欄與抽屜", () => {
  it("桌機：可收合的左側紀錄欄（新對話、搜尋、時間分組、開啟、刪除），收合狀態存在瀏覽器", async () => {
    factoryBackend();
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    expect(where).toMatch(/^\/c\//);
    const side = document.querySelector(".side")!;
    expect(within(side as HTMLElement).getByText("今天")).toBeTruthy();
    expect(within(side as HTMLElement).getByText("連接法蘭有哪些公差要求？")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("收合對話紀錄"));
    expect(document.querySelector(".side.is-mini")).not.toBeNull();
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).collapsed).toBe(true));
    fireEvent.click(screen.getByLabelText("展開對話紀錄"));
    // 新對話 → 搜尋 → 開啟
    fireEvent.click(screen.getByText("新對話", { selector: ".side__new span" }));
    expect(where).toBe("/");
    fireEvent.change(screen.getByLabelText("搜尋對話紀錄"), { target: { value: "不存在的字" } });
    expect(screen.getByText("找不到符合的對話。")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("搜尋對話紀錄"), { target: { value: "法蘭" } });
    fireEvent.click(document.querySelector(".side__open")!);
    expect(where).toMatch(/^\/c\//);
    expect(document.querySelector(".thread")?.textContent).toContain("外徑公差 ±0.02 mm");
  });

  it("刪除紀錄：清掉畫面、記憶體與瀏覽器裡的那一段，正在看的那段會回到入口", async () => {
    factoryBackend();
    renderApp();
    await ask("梵谷畫這幅畫的時候在哪裡？");
    await idle();
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs).toHaveLength(1));
    fireEvent.click(screen.getByLabelText(/^刪除「/));
    expect(window.confirm).toHaveBeenCalled();
    expect(where).toBe("/");
    expect(screen.getByText("還沒有對話紀錄。")).toBeTruthy();
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs).toHaveLength(0));
  });

  it("手機：沒有常駐側欄，紀錄改成可開關的抽屜", async () => {
    viewport.width = 390;
    factoryBackend();
    renderApp();
    expect(document.querySelector(".side")).toBeNull();
    fireEvent.click(screen.getByLabelText("打開對話紀錄"));
    expect(screen.getByRole("dialog", { name: "對話紀錄" })).toBeTruthy();
    fireEvent.click(screen.getByLabelText("關閉對話紀錄"));
    expect(screen.queryByRole("dialog", { name: "對話紀錄" })).toBeNull();
    fireEvent.click(screen.getByLabelText("打開對話紀錄"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "對話紀錄" })).toBeNull();
  });
});

describe("入口 → 模組：跳轉、雙窗格、成果歷程、開啟紀錄", () => {
  it("工廠問題：簡介＋圖紙縮圖＋「進入工廠模組」；進入後延續同一段對話，右側是圖紙", async () => {
    factoryBackend();
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    expect(document.querySelector(".subject--drawing img")).not.toBeNull();
    const jump = document.querySelector(".jump--factory") as HTMLElement;
    expect(jump.textContent).toContain("進入工廠模組");
    expect(document.querySelectorAll(".trace__stage")).toHaveLength(7);
    const convId = where.split("/")[2];
    fireEvent.click(jump);
    await waitFor(() => expect(where).toBe(`/factory/${convId}`));
    expect(screen.getByText("連接法蘭有哪些公差要求？", { selector: ".msg__bubble" })).toBeTruthy();
    await waitFor(() => expect(document.querySelector(".showcase__kind")?.textContent).toContain("圖紙"));
    await waitFor(() => expect(document.querySelector(".titleblock")?.textContent).toContain("FLG-2002"));
  });

  it("畫作問題：「進入藝術模組」；一般問答（閒聊）不給任何跳轉", async () => {
    const t = factoryBackend();
    renderApp();
    await ask("梵谷畫這幅畫的時候在哪裡？");
    await idle();
    expect(document.querySelector(".jump--art")?.textContent).toContain("進入藝術模組");
    t.on("POST", "/agent/route", () => json(route({ outcome: "short_circuit", short_circuit: { stage: 2, by: "地端", reply: "我可以幫你找畫。" } })));
    await ask("今天天氣如何？");
    await idle();
    const last = [...document.querySelectorAll(".msg--ai")].at(-1)!;
    expect(last.textContent).toContain("我可以幫你找畫。");
    expect(last.querySelector(".jump")).toBeNull();
  });

  it("雙窗格：預設 50%，拖曳限制在 30%–70%，方向鍵微調，雙擊回到 50%", async () => {
    factoryBackend();
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    fireEvent.click(document.querySelector(".jump--factory")!);
    await waitFor(() => expect(document.querySelector(".msplit")).not.toBeNull());
    const sep = screen.getByRole("separator");
    const body = document.querySelector(".mbody") as HTMLElement;
    body.getBoundingClientRect = () => ({ left: 0, width: 1000, top: 0, height: 800, right: 1000, bottom: 800, x: 0, y: 0, toJSON: () => ({}) });
    expect(sep.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.pointerDown(sep, { pointerId: 1 });
    act(() => {
      sep.dispatchEvent(new MouseEvent("pointermove", { clientX: 900, bubbles: true }));
    });
    expect(sep.getAttribute("aria-valuenow")).toBe("70");
    act(() => {
      sep.dispatchEvent(new MouseEvent("pointermove", { clientX: 100, bubbles: true }));
    });
    expect(sep.getAttribute("aria-valuenow")).toBe("30");
    act(() => {
      sep.dispatchEvent(new MouseEvent("pointermove", { clientX: 620, bubbles: true }));
      sep.dispatchEvent(new MouseEvent("pointerup", { bubbles: true }));
    });
    expect(sep.getAttribute("aria-valuenow")).toBe("62");
    expect(body.style.getPropertyValue("--split")).toBe("62%");
    fireEvent.keyDown(sep, { key: "ArrowLeft" });
    expect(sep.getAttribute("aria-valuenow")).toBe("60");
    fireEvent.doubleClick(sep);
    expect(sep.getAttribute("aria-valuenow")).toBe("50");
  });

  it("成果歷程：新成果自動成為目前成果，點縮圖、← → 切換；回入口再開紀錄回到模組與當時的成果", async () => {
    factoryBackend();
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    fireEvent.click(document.querySelector(".jump--factory")!);
    await waitFor(() => expect(document.querySelector(".showcase")).not.toBeNull());
    const url = where;
    await ask("法蘭還剩幾件可以出貨？");
    await idle();
    await waitFor(() => expect(document.querySelectorAll(".strip__item")).toHaveLength(2));
    // 新的查詢結果自動放上展示區
    expect(document.querySelector(".showcase__kind")?.textContent).toMatch(/庫存|查詢/);
    expect(document.querySelector(".view--data table")?.textContent).toContain("一廠成品倉");
    fireEvent.click(document.querySelectorAll(".strip__item")[0]);
    expect(document.querySelector(".showcase__kind")?.textContent).toContain("圖紙");
    const show = document.querySelector(".showcase") as HTMLElement;
    fireEvent.keyDown(show, { key: "ArrowRight" });
    expect(document.querySelector(".showcase__kind")?.textContent).toMatch(/庫存|查詢/);
    fireEvent.keyDown(show, { key: "ArrowLeft" });
    expect(document.querySelector(".showcase__kind")?.textContent).toContain("圖紙");
    // 回入口（保留這段對話）→ 新對話 → 從紀錄打開：回到工廠模組，成果歷程與目前成果都還在
    fireEvent.click(screen.getByTitle("回到入口（保留這段對話）"));
    expect(where).toMatch(/^\/c\//);
    fireEvent.click(screen.getByText("新對話", { selector: ".side__new span" }));
    fireEvent.click(document.querySelector(".side__open")!);
    await waitFor(() => expect(where).toBe(url));
    expect(document.querySelectorAll(".strip__item")).toHaveLength(2);
    expect(document.querySelector(".showcase__kind")?.textContent).toContain("圖紙");
    // 存檔裡沒有憑證、交接票或內部數字
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs[0].module).toBe("factory"));
    const saved = localStorage.getItem(STORAGE_KEY)!;
    expect(saved).not.toContain("ticket-must-not-be-stored");
    expect(saved).not.toContain("一廠成品倉");
    expect(saved).not.toContain("±0.02");
  });

  it("重新整理後打開紀錄：回到模組；工廠內部資料要以目前身分重新查詢", async () => {
    factoryBackend();
    const first = renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    fireEvent.click(document.querySelector(".jump--factory")!);
    await waitFor(() => expect(document.querySelector(".showcase")).not.toBeNull());
    const url = where;
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs[0].module).toBe("factory"));
    first.unmount();
    renderApp(url);
    await waitFor(() => expect(document.querySelector(".showcase__kind")?.textContent).toContain("圖紙"));
    expect(screen.getAllByText("以目前身分重新查詢").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("±0.02");
  });
});
