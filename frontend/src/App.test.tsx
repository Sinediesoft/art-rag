import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { STORAGE_KEY, UNCONFIRMED_KEY } from "./shell/persist";
import { ShellProvider } from "./shell/store";
import { viewport } from "./test/setup";
import { account, accounts, baseHandlers, done, factoryRoute, json, liveSse, mockTransport, route, sources, sse } from "./test/transport";

// 照片前處理用 canvas，jsdom 沒有：直接把檔案交給上傳
vi.mock("./lib/image", () => ({ preprocessImage: async (f: File) => f }));

let where = "/";
let whereSearch = "";
function Where() {
  const l = useLocation();
  where = l.pathname;
  whereSearch = l.search;
  return null;
}

/** 最近一次 renderApp 的 QueryClient（檢查共用快取有沒有被舊回應寫回） */
let lastQc!: QueryClient;

function renderApp(path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  lastQc = qc;
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

/** 和使用者一樣：等身分確認、輸入框解鎖再送出 */
const ready = () => waitFor(() => expect(document.querySelector(".composer__lock")).toBeNull());

const ask = async (text: string) => {
  await ready();
  await sendNow(text);
};

/** 不等解鎖直接按 Enter（測試「鎖住時送不出去」用） */
const sendNow = async (text: string) => {
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

describe("切換身分時還在跑的請求（舊 JWT）", () => {
  it("串流中切成訪客：中止並收起，之後舊串流再送來的資料不會出現在畫面與存檔；切換後的新提問照常完成", async () => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    t.on("POST", "/auth/switch", ({ body }) => {
      current = (body as { account_id: string }).account_id;
      return json(accounts(current));
    });
    const live = liveSse();
    t.on("POST", "/inventory/ask", ({ signal }) => live.respond(signal));
    renderApp();
    await waitFor(() => expect(document.querySelector(".topbar__account")?.textContent).toContain("生管"));
    await ask("法蘭還剩幾件可以出貨？");
    await waitFor(() => expect(t.calls.some((c) => c.path === "/inventory/ask")).toBe(true));
    live.push("sql", { attempt: 1, sql: "SELECT 1", ok: true, error: null });
    live.push("result", { columns: ["倉庫", "可用"], rows: [["SYNTH_WAREHOUSE", 99999]], row_count: 1, truncated: false, exec_ms: 2 });
    await waitFor(() => expect(document.querySelector(".msg--ai")?.textContent).toContain("1 筆"));
    // 切成訪客
    fireEvent.click(document.querySelector(".topbar__account")!);
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitemradio", { name: /訪客/ }));
    });
    await waitFor(() => expect(document.querySelector(".thread")?.textContent).toContain("進行中的請求已中止"));
    // 舊串流晚到的事件
    act(() => {
      live.push("token", { text: "SYNTH_ANSWER 還有 99999 件。" });
      live.push("done", { request_id: "r" });
      live.close();
    });
    await new Promise((r) => setTimeout(r, 50));
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("SYNTH_ANSWER");
    expect(text).not.toContain("99999");
    expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped");
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("切換身分，已中止這一輪"));
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain("99999");
    // 切換之後才送出的提問用的是新身分的 JWT（後端回的帳號是訪客），不會被中止
    t.on("POST", "/agent/route", ({ body }) => json(route({ question: (body as { question: string }).question, account: account(current) })));
    await ask("梵谷畫這幅畫的時候在哪裡？");
    await idle();
    expect([...document.querySelectorAll(".msg--ai")].at(-1)?.getAttribute("data-phase")).toBe("done");
    expect(document.querySelector(".thread")?.textContent).toContain("1889 年在聖雷米");
  });
});

describe("切換身分時路由還沒回來的請求（審查報告的延遲 switch＋延遲 route）", () => {
  it("切換開始就中止在飛的請求；切換中不接受新提問；舊身分的路由晚到也不寫回", async () => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    let releaseSwitch!: () => void;
    t.on("POST", "/auth/switch", ({ body }) =>
      new Promise<Response>((ok) => {
        releaseSwitch = () => {
          current = (body as { account_id: string }).account_id;
          ok(json(accounts(current)));
        };
      }),
    );
    let releaseRoute!: () => void;
    t.on("POST", "/agent/route", () =>
      new Promise<Response>((ok) => {
        // 生管的 JWT 授權過的結果：確認卡上會有圖紙名稱與機密等級（合成值）
        releaseRoute = () =>
          ok(
            json(
              factoryRoute({
                question: "把附圖轉成 3D",
                intent: "reconstruct",
                gate: "confirm",
                dispatch: { module: "reconstruct", part_id: "mfg-002", part_label: "SYNTH_PRIVATE_PART", artwork_id: null, question: "把附圖轉成 3D" },
              }),
            ),
          );
      }),
    );
    renderApp();
    await waitFor(() => expect(document.querySelector(".topbar__account")?.textContent).toContain("生管"));
    await ask("把附圖轉成 3D");
    await waitFor(() => expect(t.calls.filter((c) => c.path === "/agent/route")).toHaveLength(1));
    // 開始切成訪客（切換回應還沒回來）
    fireEvent.click(document.querySelector(".topbar__account")!);
    act(() => {
      fireEvent.click(screen.getByRole("menuitemradio", { name: /訪客/ }));
    });
    await waitFor(() => expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped"));
    // 切換中：輸入框鎖住，送不出去
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("切換身分中"));
    await sendNow("再問一句");
    expect(t.calls.filter((c) => c.path === "/agent/route")).toHaveLength(1);
    // 切換完成，接著舊身分的路由才回來
    await act(async () => {
      releaseSwitch();
    });
    await waitFor(() => expect(document.querySelector(".topbar__account")?.textContent).toContain("訪客"));
    await act(async () => {
      releaseRoute();
      await new Promise((r) => setTimeout(r, 30));
    });
    expect(document.body.textContent).not.toContain("SYNTH_PRIVATE_PART");
    expect(document.querySelector(".taskcard")).toBeNull();
    expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped");
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("切換身分，已中止這一輪"));
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain("SYNTH_PRIVATE_PART");
  });

  it("憑證過期改發訪客：路由回來的帳號不是目前的身分，就不顯示、不分派", async () => {
    const t = factoryBackend();
    t.on("POST", "/agent/route", () =>
      json(
        factoryRoute({
          account: account("guest"),
          question: "把附圖轉成 3D",
          intent: "reconstruct",
          gate: "confirm",
          dispatch: { module: "reconstruct", part_id: "mfg-002", part_label: "SYNTH_OTHER_IDENTITY", artwork_id: null, question: "把附圖轉成 3D" },
        }),
      ),
    );
    renderApp();
    await waitFor(() => expect(document.querySelector(".topbar__account")?.textContent).toContain("生管"));
    await ask("把附圖轉成 3D");
    await waitFor(() => expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped"));
    expect(document.body.textContent).not.toContain("SYNTH_OTHER_IDENTITY");
    expect(document.querySelector(".taskcard")).toBeNull();
  });
});

describe("身分還沒確認、憑證更新（審查報告：首次 accounts 未成功 → 生管串流 → 首次 accounts 回訪客 → 舊事件晚到）", () => {
  it("剛打開頁面、/auth/accounts 還沒回來：輸入框鎖住、送不出任何提問；確認後才解鎖", async () => {
    const t = factoryBackend();
    let releaseAccounts!: () => void;
    t.on("GET", "/auth/accounts", () => new Promise<Response>((ok) => (releaseAccounts = () => ok(json(accounts("planner"))))));
    renderApp();
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("正在確認身分"));
    await sendNow("法蘭還剩幾件可以出貨？");
    expect(t.calls.some((c) => c.path === "/agent/route")).toBe(false);
    await act(async () => {
      releaseAccounts();
    });
    await ask("梵谷畫這幅畫的時候在哪裡？");
    await idle();
    expect(t.calls.filter((c) => c.path === "/agent/route")).toHaveLength(1);
    expect(document.querySelector(".thread")?.textContent).toContain("1889 年在聖雷米");
  });

  it("串流中憑證更新、重新確認是訪客：舊身分的串流中止，晚到的事件不寫回畫面與存檔；確認前鎖住", async () => {
    const t = factoryBackend();
    let current = "planner";
    let releaseAccounts: (() => void) | null = null;
    t.on("GET", "/auth/accounts", () =>
      releaseAccounts === null ? json(accounts(current)) : new Promise<Response>((ok) => (releaseAccounts = () => ok(json(accounts(current))))),
    );
    const live = liveSse();
    t.on("POST", "/inventory/ask", ({ signal }) => live.respond(signal));
    renderApp();
    await ask("法蘭還剩幾件可以出貨？");
    await waitFor(() => expect(t.calls.some((c) => c.path === "/inventory/ask")).toBe(true));
    live.push("sql", { attempt: 1, sql: "SELECT 1", ok: true, error: null });
    // 憑證過期：client.ts 重新取得憑證（後端改發訪客），重新確認身分的請求先扣住
    current = "guest";
    releaseAccounts = () => undefined;
    act(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: "TOKEN_EXPIRED" }));
    });
    await waitFor(() => expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped"));
    await waitFor(() => expect(document.querySelector(".composer__lock")?.textContent).toContain("正在確認身分"));
    // 舊身分串流晚到的事件（合成值）
    act(() => {
      live.push("result", { columns: ["倉庫", "可用"], rows: [["SYNTH_PRIVATE_INVENTORY_ROWS", 77777]], row_count: 1, truncated: false, exec_ms: 2 });
      live.push("token", { text: "SYNTH_PRIVATE_ANSWER" });
      live.push("done", { request_id: "r" });
      live.close();
    });
    await act(async () => {
      releaseAccounts!();
    });
    await waitFor(() => expect(document.querySelector(".topbar__account")?.textContent).toContain("訪客"));
    await ready();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("SYNTH_PRIVATE_INVENTORY_ROWS");
    expect(text).not.toContain("SYNTH_PRIVATE_ANSWER");
    expect(text).not.toContain("77777");
    expect(document.querySelector(".msg--ai")?.getAttribute("data-phase")).toBe("stopped");
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("切換身分，已中止這一輪"));
    expect(localStorage.getItem(STORAGE_KEY)).not.toMatch(/SYNTH_PRIVATE|77777/);
  });
});

describe("憑證更新時已完成的非公開成果（審查報告：完成 SQL／私有問答／3D → token-renewed → accounts 一直沒回來）", () => {
  it("對話與展示區都立刻撤下舊身分的完成結果，不等身分重新確認", async () => {
    const t = factoryBackend();
    t.on("POST", "/agent/route", ({ body }) => {
      const q = (body as { question: string }).question;
      if (q.includes("幾件")) return json(route({ question: q, intent: "data_query", intent_label: "庫存・訂單・工單查詢", dispatch: { artwork_id: null, part_id: null, question: q } }));
      if (q.includes("3D"))
        return json(factoryRoute({ question: q, intent: "reconstruct", intent_label: "3D 重建", gate: "confirm", dispatch: { module: "reconstruct", part_id: "mfg-002", part_label: "連接法蘭", artwork_id: null, question: q } }));
      return json(factoryRoute({ question: q }));
    });
    t.on("POST", "/cad/reconstruct", () =>
      sse([
        ["meta", { request_id: "r", job_id: "job-9", strategy: "ortho2cad", model: "mock", image_id: null, part: null, identified: null, layout: "kb", input_url: "", input_size: [1, 1], scale_to: null, scale_source: null }],
        ["executing", {}],
        ["result", { ok: true, error: null, code: "SYNTH_CAD_CODE", valid: true, repaired: false, raw_dims: null, dims: { width: 111, depth: 222, height: 33 }, scale: 1, volume: 1, faces: 12, iou: 0.99, iou_bbox: 0.99, files: { "model.stl": "/api/v1/cad/jobs/job-9/model.stl" } }],
        ["done", { request_id: "r", job_id: "job-9", strategy: "ortho2cad", model: "mock", latency_ms: { first_token: 1, generation: 1, exec: 1, total: 2800 }, tokens: { input: 1, output: 1 }, egress: { images: 0, chunks: 0, bytes: 0 } }],
      ]),
    );
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    fireEvent.click(document.querySelector(".jump--factory")!);
    await waitFor(() => expect(document.querySelector(".showcase")).not.toBeNull());
    await ask("法蘭還剩幾件可以出貨？");
    await idle();
    await ask("把連接法蘭轉成 3D");
    await idle();
    fireEvent.click(screen.getByText("開始轉換"));
    await waitFor(() => expect([...document.querySelectorAll(".strip__item")].some((b) => b.textContent?.includes("3D"))).toBe(true));
    const before = document.body.textContent ?? "";
    expect(before).toContain("±0.02");
    expect(before).toContain("111×222×33");
    // 憑證過期：client.ts 已改拿訪客憑證；重新確認身分的 /auth/accounts 一直沒回來，後端對舊身分的資料改回 403
    t.on("GET", "/auth/accounts", () => new Promise<Response>(() => undefined));
    const denied = () => json({ error: { code: "DATA_SCOPE_DENIED", message: "看不到", request_id: "r" } }, 403);
    t.on("GET", "/parts/mfg-002", denied);
    t.on("GET", "/cad/jobs/job-9", denied);
    act(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: "TOKEN_EXPIRED" }));
    });
    await waitFor(() => expect(document.querySelector(".composer__lock")?.textContent).toContain("正在確認身分"));
    // 左邊對話、右邊展示區、縮圖歷程都不能再有舊身分的結果
    for (const kind of ["圖紙", "查詢", "庫存", "3D"]) {
      const item = [...document.querySelectorAll(".strip__item")].find((b) => b.textContent?.includes(kind));
      if (item) {
        fireEvent.click(item);
        await new Promise((r) => setTimeout(r, 20));
      }
      const text = document.body.textContent ?? "";
      expect(text).not.toContain("±0.02");
      expect(text).not.toContain("一廠成品倉");
      expect(text).not.toContain("還有 15 件");
      expect(text).not.toContain("111×222×33");
      expect(text).not.toContain("SYNTH_CAD_CODE");
      expect(text).not.toContain("FLG-2002");
    }
    expect(document.querySelector(".view--data table")).toBeNull();
    expect(document.querySelector(".taskcard")).toBeNull();
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).not.toMatch(/±0\.02|一廠成品倉|還有 15 件|111/));
  });
});

describe("入口：沒有模組可以跳轉時，功能頁的交接連結要在", () => {
  it("批次辨識：入口給「打開批次辨識」，點了到 /batch", async () => {
    const t = factoryBackend();
    t.on("POST", "/agent/route", () =>
      json(route({ question: "批次辨識一批照片", intent: "batch_identify", intent_label: "批次辨識", dispatch: { artwork_id: null, part_id: null, path: "/batch", question: "批次辨識一批照片" } })),
    );
    renderApp();
    await ask("批次辨識一批照片");
    await idle();
    const last = [...document.querySelectorAll(".msg--ai")].at(-1)!;
    expect(last.querySelector(".jump")).toBeNull();
    const link = within(last as HTMLElement).getByText("打開批次辨識").closest("a")!;
    expect(link.getAttribute("href")).toBe("/batch");
    fireEvent.click(link);
    await waitFor(() => expect(where).toBe("/batch"));
  });

  it("比對不到的照片：入口給「看照片辨識細節」與「拍照建檔」兩邊（能用工廠領域的身分才有圖紙）", async () => {
    const t = factoryBackend();
    t.on("POST", "/images", () => json({ image_id: "img-9" }));
    t.on("POST", "/agent/route", () =>
      json(route({ question: "", intent: "art_search", photo: { kind: "unknown", id: null, label: "", domain: "mfg" }, dispatch: { artwork_id: null, question: "" } })),
    );
    renderApp();
    await act(async () => {
      fireEvent.change(screen.getByTestId("composer-file"), { target: { files: [new File(["x"], "a.jpg", { type: "image/jpeg" })] } });
    });
    await screen.findByAltText("附加的照片");
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });
    await idle();
    const last = [...document.querySelectorAll(".msg--ai")].at(-1) as HTMLElement;
    expect(last.querySelector(".jump")).toBeNull();
    expect(within(last).getByText(/看照片辨識細節/).closest("a")!.getAttribute("href")).toBe("/search?image=img-9");
    expect(within(last).getByText("拍照建檔：這是一幅畫").closest("a")!.getAttribute("href")).toBe("/artworks/intake?image=img-9");
    expect(within(last).getByText("拍照建檔：這是一張圖紙").closest("a")!.getAttribute("href")).toBe("/drawings/intake?image=img-9");
  });

  it("降級「查無資料」：沒有跳轉，也沒有功能頁連結", async () => {
    const t = factoryBackend();
    const src = sources("機密");
    src.post_filter.gate = { ...src.post_filter.gate, passed: false, message: "查無資料" };
    t.on("POST", "/chat", () => sse([["sources", src], ["token", { text: "查無資料" }], ["done", done({ degraded: true })]]));
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    const last = [...document.querySelectorAll(".msg--ai")].at(-1)!;
    expect(last.querySelector(".jump")).toBeNull();
    expect(last.querySelector(".deeplinks")).toBeNull();
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

/** 功能頁頁首、模組頁首的身分按鈕（顯示目前身分名稱的那一顆） */
const accountButton = (label: string) => [...document.querySelectorAll<HTMLElement>(".mtop__btn")].find((b) => b.textContent?.includes(label))!;

const sqlDone = {
  request_id: "r",
  strategy_requested: "hybrid",
  strategy_used: "hybrid",
  model: "mock",
  fallback: false,
  fallback_reason: null,
  prompt_version: "v",
  answer_prompt_version: "v",
  attempts: 1,
  latency_ms: { first_token: 1, sql: 1, exec: 1, answer: 1, total: 4 },
  tokens: { input: 1, output: 1 },
  egress: { images: 0, chunks: 0, bytes: 0 },
};

/** 合成的庫存結果（SYNTH 開頭，任何時候都不該留在畫面上） */
const synthSql = (live: ReturnType<typeof liveSse>) => {
  live.push("sql", { attempt: 1, sql: "SELECT SYNTH_SQL", ok: true, error: null });
  live.push("result", { columns: ["倉庫", "可用"], rows: [["SYNTH_WAREHOUSE", 99999]], row_count: 1, truncated: false, exec_ms: 2 });
};

describe("功能頁的身分邊界（架構審查 F1：功能頁自己的 state 不在 React Query 快取裡）", () => {
  const inventoryAsk = async (q: string) => {
    const input = await screen.findByPlaceholderText(/連接法蘭還有幾件可以出貨/);
    fireEvent.change(input, { target: { value: q } });
    await act(async () => {
      fireEvent.submit(input.closest("form")!);
    });
  };

  it("/inventory 已完成的 SQL：開始切成訪客的當下就撤下，切換完成後也不會回來", async () => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    let releaseSwitch!: () => void;
    t.on("POST", "/auth/switch", ({ body }) =>
      new Promise<Response>((ok) => {
        releaseSwitch = () => {
          current = (body as { account_id: string }).account_id;
          ok(json(accounts(current)));
        };
      }),
    );
    const live = liveSse();
    t.on("POST", "/inventory/ask", ({ signal }) => live.respond(signal));
    renderApp("/inventory");
    await waitFor(() => expect(accountButton("生管")).toBeTruthy());
    await inventoryAsk("法蘭還剩幾件可以出貨？");
    await waitFor(() => expect(t.calls.some((c) => c.path === "/inventory/ask")).toBe(true));
    synthSql(live);
    act(() => {
      live.push("token", { text: "SYNTH_ANSWER 還有 99999 件。" });
      live.push("done", sqlDone);
      live.close();
    });
    await waitFor(() => expect(document.body.textContent).toContain("SYNTH_ANSWER"));
    expect(document.body.textContent).toContain("SYNTH_WAREHOUSE");
    // 切成訪客：切換回應還沒回來
    fireEvent.click(accountButton("生管"));
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitemradio", { name: /訪客/ }));
    });
    await waitFor(() => expect(screen.getByText(/切換身分中/)).toBeTruthy());
    for (const s of ["SYNTH_ANSWER", "SYNTH_WAREHOUSE", "SYNTH_SQL", "99999"]) expect(document.body.textContent).not.toContain(s);
    await act(async () => releaseSwitch());
    await waitFor(() => expect(accountButton("訪客")).toBeTruthy());
    // 功能頁以新身分重建：輸入框回來了，舊結果沒有
    await screen.findByPlaceholderText(/連接法蘭還有幾件可以出貨/);
    for (const s of ["SYNTH_ANSWER", "SYNTH_WAREHOUSE", "SYNTH_SQL", "99999"]) expect(document.body.textContent).not.toContain(s);
  });

  it("/inventory 串流中憑證更新：當下撤下、請求中止；傳輸層不理會中止、舊事件晚到也不能讓內容回來", async () => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    const live = liveSse();
    let signal: AbortSignal | null | undefined;
    // 不把 signal 交給串流：模擬「中止了，但資料還是送到了」
    t.on("POST", "/inventory/ask", (req) => {
      signal = req.signal;
      return live.respond(null);
    });
    renderApp("/inventory");
    await waitFor(() => expect(accountButton("生管")).toBeTruthy());
    await inventoryAsk("法蘭還剩幾件可以出貨？");
    await waitFor(() => expect(signal).toBeTruthy());
    synthSql(live);
    await waitFor(() => expect(document.body.textContent).toContain("SYNTH_WAREHOUSE"));
    // 憑證過期：client.ts 改拿訪客憑證
    current = "guest";
    act(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: "TOKEN_EXPIRED" }));
    });
    await waitFor(() => expect(document.body.textContent).not.toContain("SYNTH_WAREHOUSE"));
    expect(signal!.aborted).toBe(true);
    act(() => {
      live.push("token", { text: "SYNTH_LATE 還有 99999 件。" });
      live.push("done", { request_id: "r" });
      live.close();
    });
    await waitFor(() => expect(accountButton("訪客")).toBeTruthy());
    await screen.findByPlaceholderText(/連接法蘭還有幾件可以出貨/);
    await new Promise((r) => setTimeout(r, 50));
    for (const s of ["SYNTH_LATE", "SYNTH_WAREHOUSE", "SYNTH_SQL", "99999"]) expect(document.body.textContent).not.toContain(s);
  });

  it("其他功能頁一樣：圖紙問答串流中切換身分，串流中止、回答撤下", async () => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    t.on("POST", "/auth/switch", ({ body }) => {
      current = (body as { account_id: string }).account_id;
      return json(accounts(current));
    });
    const live = liveSse();
    let signal: AbortSignal | null | undefined;
    t.on("POST", "/chat", (req) => {
      signal = req.signal;
      return live.respond(null);
    });
    renderApp("/drawings/mfg-002/chat");
    const input = await screen.findByPlaceholderText(/問這張圖紙的製程/);
    fireEvent.change(input, { target: { value: "外徑公差多少？" } });
    await act(async () => {
      fireEvent.submit(input.closest("form")!);
    });
    await waitFor(() => expect(signal).toBeTruthy());
    act(() => {
      live.push("sources", sources("機密"));
      live.push("token", { text: "SYNTH_TOLERANCE ±0.02 mm" });
    });
    await waitFor(() => expect(document.body.textContent).toContain("SYNTH_TOLERANCE"));
    fireEvent.click(accountButton("生管"));
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitemradio", { name: /訪客/ }));
    });
    await waitFor(() => expect(accountButton("訪客")).toBeTruthy());
    expect(signal!.aborted).toBe(true);
    act(() => {
      live.push("token", { text: "SYNTH_LATE" });
      live.close();
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(document.body.textContent).not.toContain("SYNTH_TOLERANCE");
    expect(document.body.textContent).not.toContain("SYNTH_LATE");
  });
});

describe("已送出的寫入（架構審查 F2：中止接收不等於撤銷交易）", () => {
  const preview = {
    request_id: "r",
    op: "adjust_stock",
    op_label: "庫存增減",
    account: account(),
    params: {},
    param_labels: { 數量: 5 },
    sources: {},
    notes: [],
    llm: {},
    missing: [],
    checks: [],
    diff: [{ label: "連接法蘭", field: "可用", before: 10, after: 15 }],
    reasons: [],
    pending_id: "pend-1",
    summary: "",
    next: "confirm",
    message: "檢查都通過",
    latency_ms: 1,
  };

  /** 進工廠模組、試算一筆修改，停在「確認寫入」 */
  const changeReady = async (over: Record<string, unknown> = {}) => {
    const t = factoryBackend();
    let current = "planner";
    t.on("GET", "/auth/accounts", () => json(accounts(current)));
    t.on("POST", "/auth/switch", ({ body }) => {
      current = (body as { account_id: string }).account_id;
      return json(accounts(current));
    });
    t.on("POST", "/agent/route", ({ body }) => {
      const q = (body as { question: string }).question;
      if (q.includes("加 5"))
        return json(factoryRoute({ question: q, intent: "modify", intent_label: "修改資料", gate: "modify", dispatch: { op: "adjust_stock", part_id: "mfg-002", part_label: "連接法蘭", artwork_id: null, question: q } }));
      return json(factoryRoute({ question: q }));
    });
    t.on("POST", "/changes/preview", () => json({ ...preview, ...over }));
    renderApp();
    await ask("連接法蘭有哪些公差要求？");
    await idle();
    fireEvent.click(document.querySelector(".jump--factory")!);
    await waitFor(() => expect(document.querySelector(".showcase")).not.toBeNull());
    await ask("連接法蘭庫存加 5");
    await idle();
    await screen.findByRole("button", { name: over.next === "approval" ? "送主管核准" : "確認寫入" });
    return { t, setCurrent: (id: string) => (current = id) };
  };
  const committed = { change_no: "CH-1", text: "SYNTH_COMMITTED", rows: [], moves: 1, op: "adjust_stock", summary: "", account: account() };
  const commits = (t: ReturnType<typeof mockTransport>) => t.calls.filter((c) => c.path === "/changes/pend-1/commit").length;

  it("送出中不能切換身分、不能刪對話；憑證更新 → 標成「結果未確認」不是「已中止」，回覆到了只說完成、不顯示內容，不自動重送", async () => {
    const { t, setCurrent } = await changeReady();
    let release!: () => void;
    t.on("POST", "/changes/pend-1/commit", () => new Promise<Response>((ok) => (release = () => ok(json(committed)))));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await screen.findByRole("button", { name: "寫入中…" });
    // 身分選單：切換被擋下
    fireEvent.click(accountButton("生管"));
    expect(screen.getByText("有寫入還沒收到結果，等結果回來再切換身分")).toBeTruthy();
    expect((screen.getByRole("menuitemradio", { name: /訪客/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(t.calls.some((c) => c.path === "/auth/switch")).toBe(false);
    // 刪掉這段對話：不刪
    fireEvent.click(screen.getByLabelText("對話紀錄"));
    fireEvent.click(screen.getAllByLabelText(/^刪除「/)[0]);
    expect(window.alert).toHaveBeenCalledWith("有寫入還沒收到結果，等結果回來再刪除這段對話。");
    expect(where).toMatch(/^\/factory\//);
    fireEvent.keyDown(document, { key: "Escape" });
    // 憑證過期、重新確認是訪客：擋不住的身分改變
    setCurrent("guest");
    act(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: "TOKEN_EXPIRED" }));
    });
    await waitFor(() => expect(document.querySelector(".thread")?.textContent).toContain("確認寫入已經送出、沒有收到結果"));
    expect(document.querySelector(".thread")?.textContent).not.toContain("切換身分，已中止這一輪");
    await waitFor(() => expect(document.querySelector(".banner[role=alert]")?.textContent).toContain("〈確認寫入〉還在等伺服器回覆"));
    // 伺服器回覆了：提示只說完成，不把內容接回畫面
    await act(async () => release());
    await waitFor(() => expect(document.querySelector(".banner[role=alert]")?.textContent).toContain("〈確認寫入〉伺服器回覆已完成"));
    expect(document.body.textContent).not.toContain("SYNTH_COMMITTED");
    expect(commits(t)).toBe(1);
    // 不能拿這一輪重新查詢（重新試算再按一次就是第二筆）
    const last = [...document.querySelectorAll(".msg--ai")].at(-1) as HTMLElement;
    expect(within(last).queryByText("以目前身分重新查詢")).toBeNull();
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("確認寫入已經送出、沒有收到結果"));
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain("SYNTH_COMMITTED");
    fireEvent.click(within(document.querySelector(".banner[role=alert]") as HTMLElement).getByText("知道了"));
    expect(document.querySelector(".banner[role=alert]")).toBeNull();
  });

  it("連線中斷（沒有收到伺服器回覆）：標成結果未確認，不再給「確認寫入」，重新整理後也不能重跑", async () => {
    const { t } = await changeReady();
    t.on("POST", "/changes/pend-1/commit", () => Promise.reject(new TypeError("network down")));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await waitFor(() => expect(document.body.textContent).toContain("結果未確認"));
    expect(screen.queryByRole("button", { name: "確認寫入" })).toBeNull();
    expect(commits(t)).toBe(1);
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("確認寫入已經送出、沒有收到結果"));
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs[0].turns.at(-1);
    expect(saved.archived.outcome).toBe("unconfirmed");
  });

  it("伺服器明確回覆失敗（4xx）：照常顯示錯誤，可以再按一次", async () => {
    const { t } = await changeReady();
    t.on("POST", "/changes/pend-1/commit", () => json({ error: { code: "PENDING_EXPIRED", message: "試算已過期", request_id: "r" } }, 409));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await waitFor(() => expect(document.body.textContent).toContain("試算已過期（PENDING_EXPIRED）"));
    expect(document.body.textContent).not.toContain("結果未確認");
    expect(screen.getByRole("button", { name: "確認寫入" })).toBeTruthy();
  });

  // 第 8 次 code review F1：沒有重新整理、沒有換身分，直接按最後一輪的「重新產生」
  const lastAi = () => [...document.querySelectorAll(".msg--ai")].at(-1) as HTMLElement;
  const routeCalls = (t: ReturnType<typeof mockTransport>) => t.calls.filter((c) => c.path === "/agent/route").length;

  it("寫入送出中：最後一輪沒有「重新產生」，也不能重跑（不會重新試算）", async () => {
    const { t } = await changeReady();
    expect(within(lastAi()).getByLabelText("重新產生")).toBeTruthy();
    t.on("POST", "/changes/pend-1/commit", () => new Promise<Response>(() => undefined));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await screen.findByRole("button", { name: "寫入中…" });
    expect(within(lastAi()).queryByLabelText("重新產生")).toBeNull();
    const before = routeCalls(t);
    await new Promise((r) => setTimeout(r, 30));
    expect(routeCalls(t)).toBe(before);
    expect(t.calls.filter((c) => c.path === "/changes/preview")).toHaveLength(1);
  });

  it.each([
    ["確認寫入", {}, "/changes/pend-1/commit"],
    ["送主管核准", { next: "approval", reasons: ["超過額度"] }, "/changes/pend-1/request-approval"],
  ])("「%s」連線中斷 → 結果未確認：沒有「重新產生」，提示與送出的紀錄留著", async (label, over, path) => {
    const { t } = await changeReady(over);
    t.on("POST", path, () => Promise.reject(new TypeError("network down")));
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(lastAi().textContent).toContain(`${label}已經送出、沒有收到結果`));
    expect(within(lastAi()).queryByLabelText("重新產生")).toBeNull();
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    expect(t.calls.filter((c) => c.path === "/changes/preview")).toHaveLength(1);
    expect(t.calls.filter((c) => c.path === path)).toHaveLength(1);
  });

  // 第 8 次 code review F2：後端先 commit 交易再讀回／寫稽核，後面出錯一樣回 500
  it.each([
    ["確認寫入", {}, "/changes/pend-1/commit"],
    ["送主管核准", { next: "approval", reasons: ["超過額度"] }, "/changes/pend-1/request-approval"],
  ])("「%s」回 500：不能當成確定失敗——結果未確認、不給再按、不能重跑，存檔也是 unconfirmed", async (label, over, path) => {
    const { t } = await changeReady(over);
    t.on("POST", path, () => json({ error: { code: "INTERNAL", message: "伺服器錯誤", request_id: "r" } }, 500));
    fireEvent.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(lastAi().textContent).toContain("結果未確認"));
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    expect(within(lastAi()).queryByLabelText("重新產生")).toBeNull();
    expect(t.calls.filter((c) => c.path === path)).toHaveLength(1);
    await waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).convs[0].turns.at(-1).archived.outcome).toBe("unconfirmed"));
  });

  // 第 9 次 code review F1：送出之後、400 ms 延遲存檔之前就重新整理
  const savedChange = () =>
    (JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{"convs":[]}').convs as { turns: { archived: { intentLabel: string; outcome: string; summary: string } }[] }[])
      .flatMap((c) => c.turns)
      .find((x) => x.archived.intentLabel === "修改資料");
  const reload = () => {
    const at = where;
    cleanup();
    renderApp(at);
  };

  it.each([
    ["確認寫入", {}, "/changes/pend-1/commit"],
    ["送主管核准", { next: "approval", reasons: ["超過額度"] }, "/changes/pend-1/request-approval"],
  ])("「%s」送出的當下就寫進瀏覽器（不等 400 ms）：立刻重新整理，還原的是結果未確認、不能重跑", async (label, over, path) => {
    const { t } = await changeReady(over);
    // 試算完成的那一版已經存過（outcome pass、可以重跑）
    await waitFor(() => expect(savedChange()?.archived.outcome).toBe("pass"));
    t.on("POST", path, () => new Promise<Response>(() => undefined));
    fireEvent.click(screen.getByRole("button", { name: label }));
    // 同一個事件裡、計時器還沒跑：存檔已經是結果未確認
    expect(savedChange()?.archived.outcome).toBe("unconfirmed");
    expect(savedChange()?.archived.summary).toContain(`${label}已經送出、沒有收到結果`);
    expect(localStorage.getItem(STORAGE_KEY)).not.toMatch(/pend-1|SYNTH/);
    reload();
    await waitFor(() => expect(lastAi()?.textContent).toContain(`${label}已經送出、沒有收到結果`));
    expect(within(lastAi()).queryByText("以目前身分重新查詢")).toBeNull();
    expect(within(lastAi()).queryByLabelText("重新產生")).toBeNull();
    expect(t.calls.filter((c) => c.path === path)).toHaveLength(1);
  });

  it("另一段對話的串流不停重設存檔計時器：送出的當下一樣先寫進瀏覽器", async () => {
    const { t } = await changeReady();
    await waitFor(() => expect(savedChange()?.archived.outcome).toBe("pass"));
    // 另一段對話開始串流，之後每 50 ms 一個字（每次都重設 400 ms 的存檔計時器）
    const live = liveSse();
    t.on("POST", "/chat", () => live.respond(null));
    fireEvent.click(screen.getByLabelText("新對話"));
    await waitFor(() => expect(where).toBe("/"));
    await ask("梵谷畫這幅畫的時候在哪裡？");
    await waitFor(() => expect(t.calls.some((c) => c.path === "/chat")).toBe(true));
    act(() => void live.push("sources", sources("機密")));
    const timer = window.setInterval(() => live.push("token", { text: "字" }), 50);
    try {
      // 回到修改資料那一段
      const item = [...document.querySelectorAll<HTMLElement>(".side__open")].find((b) => b.textContent?.includes("連接法蘭有哪些公差要求"))!;
      fireEvent.click(item);
      const btn = await screen.findByRole("button", { name: "確認寫入" });
      t.on("POST", "/changes/pend-1/commit", () => new Promise<Response>(() => undefined));
      fireEvent.click(btn);
      expect(savedChange()?.archived.outcome).toBe("unconfirmed");
      reload();
      await waitFor(() => expect(lastAi()?.textContent).toContain("確認寫入已經送出、沒有收到結果"));
      expect(within(lastAi()).queryByText("以目前身分重新查詢")).toBeNull();
    } finally {
      window.clearInterval(timer);
      live.close();
    }
  });

  it("瀏覽器存不進去（空間不足）：不送出寫入，說明原因", async () => {
    const { t } = await changeReady();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await waitFor(() => expect(lastAi().textContent).toContain("記不下「已送出」的狀態"));
    expect(t.calls.some((c) => c.path === "/changes/pend-1/commit")).toBe(false);
    expect(screen.getByRole("button", { name: "確認寫入" })).toBeTruthy();
  });

  // 第 10 次 code review F1：兩個分頁共用 localStorage，另一個分頁手上的舊快照（試算完成）存檔會蓋掉「已送出」
  const savedChangeOutcome = () => savedChange()?.archived.outcome;

  it("「確認寫入」：分頁 A 送出 → A、B 依序 pagehide（B 用舊快照蓋回 pass）→ A 重新載入：一樣是結果未確認、不能重跑；B 手上那一輪也不能重新查詢", async () => {
    const { t } = await changeReady();
    await waitFor(() => expect(savedChangeOutcome()).toBe("pass"));
    const at = where;
    // 分頁 B：這時候打開，讀到試算完成（pass）的紀錄
    const b = renderApp("/");
    t.on("POST", "/changes/pend-1/commit", () => new Promise<Response>(() => undefined));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    expect(savedChangeOutcome()).toBe("unconfirmed");
    // A 先離開（寫回 unconfirmed），B 接著離開（舊快照蓋回 pass）
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(savedChangeOutcome()).toBe("pass");
    // 另一個分頁的標記變更（瀏覽器對其他分頁發 storage 事件）：B 手上那一輪立刻不能重新查詢
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: UNCONFIRMED_KEY }));
    });
    const open = [...b.container.querySelectorAll<HTMLElement>(".side__open")].find((x) => x.textContent?.includes("連接法蘭有哪些公差要求"))!;
    fireEvent.click(open);
    await waitFor(() => expect([...b.container.querySelectorAll(".msg--ai")].at(-1)?.textContent).toContain("確認寫入已經送出、沒有收到結果"));
    const bLast = [...b.container.querySelectorAll(".msg--ai")].at(-1) as HTMLElement;
    expect(within(bLast).queryByText("以目前身分重新查詢")).toBeNull();
    // A 重新載入
    cleanup();
    renderApp(at);
    await waitFor(() => expect(lastAi()?.textContent).toContain("確認寫入已經送出、沒有收到結果"));
    expect(within(lastAi()).queryByText("以目前身分重新查詢")).toBeNull();
    expect(within(lastAi()).queryByLabelText("重新產生")).toBeNull();
    expect(t.calls.filter((c) => c.path === "/changes/pend-1/commit")).toHaveLength(1);
    expect(localStorage.getItem(UNCONFIRMED_KEY)).not.toMatch(/pend-1|SYNTH/);
  });

  it("「送主管核准」：分頁 B 的 400 ms 存檔用舊快照蓋回 pass → 重新載入仍是結果未確認、不能重跑", async () => {
    const { t } = await changeReady({ next: "approval", reasons: ["超過額度"] });
    await waitFor(() => expect(savedChangeOutcome()).toBe("pass"));
    const at = where;
    const b = renderApp("/");
    t.on("POST", "/changes/pend-1/request-approval", () => new Promise<Response>(() => undefined));
    fireEvent.click(screen.getByRole("button", { name: "送主管核准" }));
    expect(savedChangeOutcome()).toBe("unconfirmed");
    // B 改了一個偏好（收合紀錄欄），觸發 B 的一般存檔
    fireEvent.click(within(b.container).getByLabelText("收合對話紀錄"));
    await waitFor(() => expect(savedChangeOutcome()).toBe("pass"));
    cleanup();
    renderApp(at);
    await waitFor(() => expect(lastAi()?.textContent).toContain("送主管核准已經送出、沒有收到結果"));
    expect(within(lastAi()).queryByText("以目前身分重新查詢")).toBeNull();
    expect(t.calls.filter((c) => c.path === "/changes/pend-1/request-approval")).toHaveLength(1);
  });

  it("結果確實回到這一輪才解除標記：完成或伺服器明確拒絕（4xx）解除；連線中斷、5xx 留著", async () => {
    const { t } = await changeReady();
    t.on("POST", "/changes/pend-1/commit", () => json({ error: { code: "PENDING_EXPIRED", message: "試算已過期", request_id: "r" } }, 409));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await waitFor(() => expect(lastAi().textContent).toContain("試算已過期"));
    await waitFor(() => expect(localStorage.getItem(UNCONFIRMED_KEY)).toBeNull());
    t.on("POST", "/changes/pend-1/commit", () => json({ error: { code: "INTERNAL", message: "伺服器錯誤", request_id: "r" } }, 500));
    fireEvent.click(screen.getByRole("button", { name: "確認寫入" }));
    await waitFor(() => expect(lastAi().textContent).toContain("結果未確認"));
    await new Promise((r) => setTimeout(r, 30));
    expect(Object.keys(JSON.parse(localStorage.getItem(UNCONFIRMED_KEY) ?? "{}"))).toHaveLength(1);
  });
});

describe("建檔頁卸載後的晚到回應（第 8 次 code review F3：共用快取與網址）", () => {
  const field = (over: Record<string, unknown> = {}) => ({
    key: "title_zh",
    label: "中文標題",
    value: "SYNTH_TITLE",
    source: "人",
    note: null,
    hint: null,
    group: null,
    read: true,
    required: true,
    kind: "text",
    options: [],
    suggestions: [],
    status: "ok",
    message: null,
    ...over,
  });
  const draft = (over: Record<string, unknown> = {}) => ({
    draft_id: "draft-1",
    domain: "art",
    status: "draft",
    created_at: "2026-10-09T00:00:00Z",
    updated_at: null,
    image_id: "img-1",
    photo_url: "/api/v1/images/img-1",
    kb_image_url: "/api/v1/images/img-1",
    item_id: "art-new-1",
    fields: [field()],
    checks: [],
    extraction: { model: null, strategy: null, ms: null, raw: null, error: null },
    can_commit: true,
    blockers: [],
    commit: null,
    item_url: null,
    egress: {},
    ...over,
  });
  /** 能收錄的身分（kb_intake）；憑證更新後改成訪客 */
  const intakeBackend = () => {
    const t = factoryBackend();
    let current = "manager";
    t.on("GET", "/auth/accounts", () => {
      const a = accounts(current);
      return json(current === "manager" ? { ...a, current: account("manager", { ops: ["kb_intake"] }) } : a);
    });
    return { t, setCurrent: (id: string) => (current = id) };
  };
  const renew = () =>
    act(() => {
      window.dispatchEvent(new CustomEvent("artrag:token-renewed", { detail: "TOKEN_EXPIRED" }));
    });
  const cached = () => JSON.stringify(lastQc.getQueryData(["intake", "draft-1"]) ?? null);

  it("建草稿的串流：離開頁面後才到的草稿，不改網址、不寫進共用快取", async () => {
    const { t } = intakeBackend();
    const live = liveSse();
    // 傳輸層不理會中止：資料照樣送到
    t.on("POST", "/intake", () => live.respond(null));
    renderApp("/artworks/intake?image=img-1");
    await waitFor(() => expect(t.calls.some((c) => c.path === "/intake")).toBe(true));
    fireEvent.click(document.querySelector(".mtop__back")!);
    await waitFor(() => expect(where).toBe("/"));
    act(() => {
      live.push("draft", draft());
      live.close();
    });
    await new Promise((r) => setTimeout(r, 50));
    expect(where).toBe("/");
    expect(whereSearch).toBe("");
    expect(cached()).toBe("null");
  });

  it("收錄送出中憑證更新：舊的收錄回應晚到，不寫回共用快取；頁首照樣記下伺服器回覆", async () => {
    const { t, setCurrent } = intakeBackend();
    t.on("GET", "/intake/draft-1", () => json(draft()));
    let release!: () => void;
    t.on("POST", "/intake/draft-1/commit", () => new Promise<Response>((ok) => (release = () => ok(json(draft({ status: "done", fields: [field({ value: "SYNTH_COMMITTED" })] }))))));
    renderApp("/artworks/intake?draft=draft-1");
    fireEvent.click(await screen.findByRole("button", { name: "收錄進知識庫（art-new-1）" }));
    await screen.findByRole("button", { name: "收錄中…" });
    const gets = () => t.calls.filter((c) => c.method === "GET" && c.path === "/intake/draft-1").length;
    const before = gets();
    setCurrent("guest");
    renew();
    await waitFor(() => expect(document.querySelector(".banner[role=alert]")?.textContent).toContain("〈照片建檔入庫〉還在等伺服器回覆"));
    // 等身分重新確認、功能頁以新身分重建並重新讀過草稿（快取已重設），舊的收錄回應才到
    await waitFor(() => expect(accountButton("訪客")).toBeTruthy());
    await waitFor(() => expect(gets()).toBeGreaterThan(before));
    await screen.findByRole("button", { name: "收錄進知識庫（art-new-1）" });
    await act(async () => release());
    await waitFor(() => expect(document.querySelector(".banner[role=alert]")?.textContent).toContain("〈照片建檔入庫〉伺服器回覆已完成"));
    await new Promise((r) => setTimeout(r, 30));
    expect(cached()).not.toContain("SYNTH_COMMITTED");
    expect(document.body.textContent).not.toContain("SYNTH_COMMITTED");
  });

  it("儲存修改期間換了身分：存檔回應晚到，不接著送出收錄", async () => {
    const { t, setCurrent } = intakeBackend();
    t.on("GET", "/intake/draft-1", () => json(draft()));
    let release!: () => void;
    t.on("PUT", "/intake/draft-1", () => new Promise<Response>((ok) => (release = () => ok(json(draft({ fields: [field({ value: "SYNTH_SAVED" })] }))))));
    t.on("POST", "/intake/draft-1/commit", () => json(draft({ status: "done" })));
    renderApp("/artworks/intake?draft=draft-1");
    const commitBtn = await screen.findByRole("button", { name: "收錄進知識庫（art-new-1）" });
    fireEvent.change(screen.getByDisplayValue("SYNTH_TITLE"), { target: { value: "改過的標題" } });
    fireEvent.click(commitBtn);
    await waitFor(() => expect(t.calls.some((c) => c.method === "PUT" && c.path === "/intake/draft-1")).toBe(true));
    setCurrent("guest");
    renew();
    await act(async () => release());
    await new Promise((r) => setTimeout(r, 50));
    expect(t.calls.some((c) => c.path === "/intake/draft-1/commit")).toBe(false);
    expect(cached()).not.toContain("SYNTH_SAVED");
  });
});
