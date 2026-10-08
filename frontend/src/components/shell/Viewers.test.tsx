import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Output } from "../../shell/outputs";
import { json, mockTransport } from "../../test/transport";
import { ModelView } from "./Viewers";

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
