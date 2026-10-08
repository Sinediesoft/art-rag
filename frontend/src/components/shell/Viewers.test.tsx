import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Output } from "../../shell/outputs";
import { ShellProvider, useShell, type Shell } from "../../shell/store";
import { json, mockTransport } from "../../test/transport";
import { ModelView, SimilarView } from "./Viewers";

// three.js 在 jsdom 畫不出來：換成記錄參數的替身，只驗證「容器量到高度、檢視器有建立」
vi.mock("../LazyModelViewer", () => ({
  ModelViewer: ({ layers, height }: { layers: { url: string }[]; height: number }) => (
    <div data-testid="model-viewer" data-url={layers[0].url} data-height={height} />
  ),
}));

const restored: Output = {
  key: "model:t1",
  kind: "model",
  domain: "factory",
  turnId: "t1",
  partId: "mfg-002",
  cadJobId: "job-1",
  title: "3D 模型",
  meta: "Ortho2CAD・從紀錄還原",
};

describe("展示區 3D：從紀錄還原（先讀 /cad/jobs，資料回來才掛上畫布容器）", () => {
  const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
  beforeEach(() => {
    // jsdom 沒有版面：只讓 3D 畫布的容器有高度
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get() {
        return (this as HTMLElement).classList?.contains("view__canvas--model") ? 480 : 0;
      },
    });
  });
  afterEach(() => {
    if (desc) Object.defineProperty(HTMLElement.prototype, "clientHeight", desc);
  });

  it("工作編號的資料晚到：容器掛上時才量，量到高度就建立 3D 檢視器", async () => {
    const t = mockTransport();
    let resolve!: (r: Response) => void;
    t.on("GET", "/cad/jobs/job-1", () => new Promise<Response>((r) => (resolve = r)));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ModelView o={restored} />
      </QueryClientProvider>,
    );
    expect(screen.getByText("讀取 3D 模型…")).toBeTruthy();
    expect(screen.queryByTestId("model-viewer")).toBeNull();
    await act(async () => {
      resolve(
        json({
          job_id: "job-1",
          meta: {},
          result: { ok: true, files: { "model.stl": "/api/v1/cad/jobs/job-1/model.stl" }, dims: { width: 100, depth: 100, height: 22 }, faces: 12, iou: 0.99 },
          done: { latency_ms: { total: 2800 } },
        }),
      );
    });
    const viewer = await screen.findByTestId("model-viewer");
    expect(viewer.getAttribute("data-url")).toBe("/api/v1/cad/jobs/job-1/model.stl");
    expect(viewer.getAttribute("data-height")).toBe("480");
    expect(document.body.textContent).toContain("100×100×22");
  });

  it("同一個快取：先成功顯示 3D，切換身分後重抓回 403 → 模型與尺寸都撤下，顯示權限錯誤", async () => {
    const t = mockTransport();
    let allowed = true;
    t.on("GET", "/cad/jobs/job-1", () =>
      allowed
        ? json({
            job_id: "job-1",
            meta: {},
            result: { ok: true, files: { "model.stl": "/api/v1/cad/jobs/job-1/model.stl" }, dims: { width: 100, depth: 100, height: 22 }, faces: 12, iou: 0.99 },
            done: { latency_ms: { total: 2800 } },
          })
        : json({ error: { code: "DATA_SCOPE_DENIED", message: "看不到", request_id: "r" } }, 403),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let shell!: Shell;
    const Grab = () => {
      shell = useShell();
      return null;
    };
    render(
      <QueryClientProvider client={qc}>
        <ShellProvider>
          <Grab />
          <ModelView o={restored} />
        </ShellProvider>
      </QueryClientProvider>,
    );
    act(() => shell.setAccount("planner", "生管"));
    expect(await screen.findByTestId("model-viewer")).toBeTruthy();
    expect(document.body.textContent).toContain("100×100×22");
    // 切成訪客：後端改回 403
    allowed = false;
    await act(async () => {
      shell.setAccount("guest", "訪客");
    });
    expect(await screen.findByText(/目前身分看不到這份資料/)).toBeTruthy();
    expect(screen.queryByTestId("model-viewer")).toBeNull();
    expect(document.body.textContent).not.toContain("100×100×22");
  });

  it("重抓失敗但快取還有舊資料：拒絕優先，不顯示舊模型", async () => {
    const t = mockTransport();
    let allowed = true;
    t.on("GET", "/cad/jobs/job-1", () =>
      allowed
        ? json({ job_id: "job-1", meta: {}, result: { ok: true, files: { "model.stl": "/x.stl" }, dims: null, faces: 1, iou: null }, done: null })
        : json({ error: { code: "DATA_SCOPE_DENIED", message: "看不到", request_id: "r" } }, 403),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ModelView o={restored} />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("model-viewer")).toBeTruthy();
    allowed = false;
    await act(async () => {
      await qc.invalidateQueries();
    });
    expect(qc.getQueryData(["cad-job", "job-1"])).toBeTruthy(); // React Query 保留舊資料
    expect(await screen.findByText(/目前身分看不到這份資料/)).toBeTruthy();
    expect(screen.queryByTestId("model-viewer")).toBeNull();
  });

  it("看不到這份 3D（403）：顯示權限錯誤，不建立檢視器", async () => {
    const t = mockTransport();
    t.on("GET", "/cad/jobs/job-1", () => json({ error: { code: "DATA_SCOPE_DENIED", message: "看不到", request_id: "r" } }, 403));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ModelView o={restored} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/目前身分看不到這份資料/)).toBeTruthy();
    expect(screen.queryByTestId("model-viewer")).toBeNull();
  });
});

describe("展示區相似作品：以目前畫作找相近（要先讀到基準畫作）", () => {
  const similar: Output = {
    key: "similar:t1",
    kind: "similar",
    domain: "art",
    turnId: "t1",
    artworkId: "met-436535",
    title: "與〈麥田與柏樹〉相近的作品",
    meta: "以文搜畫・依風格標籤",
  };
  const detail = {
    id: "met-436535",
    title: { zh: "麥田與柏樹", en: "Wheat Field with Cypresses" },
    artist: { zh: "梵谷", en: "Vincent van Gogh" },
    date_text: "1889",
    medium: "油彩",
    collection: "Met",
    image: { width: 1, height: 1 },
    source_url: "",
    descriptions: [],
    style_tags: ["後印象派", "厚塗"],
    image_url: "/api/v1/images/met-436535.jpg",
    thumb_url: "/api/v1/images/met-436535.jpg",
  };
  const summary = (id: string, title: string) => ({
    id,
    title_zh: title,
    artist_zh: "梵谷",
    date_text: "1889",
    collection: "Met",
    image_url: `/api/v1/images/${id}.jpg`,
    thumb_url: `/api/v1/images/${id}.jpg`,
    style_tags: [],
  });
  const view = () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SimilarView o={similar} onAsk={() => {}} />
      </QueryClientProvider>,
    );
  };

  it.each([
    [403, "DATA_SCOPE_DENIED", /目前身分看不到這份資料/],
    [404, "NOT_FOUND", /找不到這份資料/],
    [500, "INTERNAL", /讀取失敗/],
  ])("基準畫作第一次讀取就失敗（%i）：顯示錯誤，不停在「搜尋相近作品…」，也不送搜尋", async (status, code, msg) => {
    const t = mockTransport();
    t.on("GET", "/artworks/met-436535", () => json({ error: { code, message: code, request_id: "r" } }, status));
    view();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(msg)).toBeTruthy();
    expect(screen.queryByText("搜尋相近作品…")).toBeNull();
    expect(t.calls.some((c) => c.path.startsWith("/search/text"))).toBe(false);
  });

  it("基準畫作讀取中：顯示「搜尋相近作品…」", () => {
    const t = mockTransport();
    t.on("GET", "/artworks/met-436535", () => new Promise<Response>(() => {}));
    view();
    expect(screen.getByText("搜尋相近作品…")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("基準畫作讀到後才以風格標籤＋媒材搜尋，結果排除本作", async () => {
    const t = mockTransport();
    t.on("GET", "/artworks/met-436535", () => json(detail));
    t.on("GET", "/search/text", () =>
      json({ results: [{ artwork: summary("met-436535", "麥田與柏樹"), score: 0.99 }, { artwork: summary("met-437980", "柏樹"), score: 0.8 }] }),
    );
    view();
    expect(await screen.findByText("柏樹")).toBeTruthy();
    expect(screen.getByText("比對基準")).toBeTruthy();
    const q = t.calls.find((c) => c.path.startsWith("/search/text"))?.path ?? "";
    expect(decodeURIComponent(q)).toContain("後印象派 厚塗 油彩");
    expect(screen.getAllByText("並排比較")).toHaveLength(1);
  });

  it("基準畫作讀到、搜尋失敗：顯示搜尋的錯誤", async () => {
    const t = mockTransport();
    t.on("GET", "/artworks/met-436535", () => json(detail));
    t.on("GET", "/search/text", () => json({ error: { code: "INTERNAL", message: "INTERNAL", request_id: "r" } }, 500));
    view();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/讀取失敗/)).toBeTruthy();
  });
});
