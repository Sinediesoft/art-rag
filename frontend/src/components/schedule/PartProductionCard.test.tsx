import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { simulateReloadForTest } from "../../api/writes";
import { json, mockTransport } from "../../test/transport";
import { WriteNotice } from "../shell/Chrome";
import { PartProductionCard } from "./PartProductionCard";

/**
 * 架構審查 c7ca88c F1：功能頁（圖紙頁）開工單收到 5xx／斷線時，結果不能確定。
 * 共用的寫入追蹤器（api/writes.ts）要接住：提示可能已經開好、同一個零件不能直接再開，核對後才能再送。
 */
const plan = {
  part_id: "mfg-002",
  part_name: "連接法蘭",
  plan_start: "2026-10-09",
  has_routing: true,
  routing: [{ op_seq: 10, kind: "自製", name: "車削", machine_type: "CNC 車床" }],
  suggestion: { qty: 10, due_on: "2026-10-20", priority: "一般", reason: "庫存低於安全庫存" },
  work_orders: [],
  skipped: [],
  schedule_run_id: null,
};
const created = { wo_no: "WO-SYNTH-1", qty: 10, due_on: "2026-10-20" };
const err = (status: number, code: string, message = code) => json({ error: { code, message, request_id: "r" } }, status);

function backend() {
  const t = mockTransport();
  t.on("GET", "/production/parts/mfg-002", () => json(plan));
  t.on("GET", "/auth/accounts", () => json({}));
  return t;
}

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WriteNotice />
        <PartProductionCard partId="mfg-002" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const submit = async () => {
  await act(async () => {
    // 開工單的表單（頁首提示的「〈開立工單〉已核對」也是按鈕，所以直接找表單）
    fireEvent.submit(document.querySelector("form")!);
  });
};
const creates = (t: ReturnType<typeof mockTransport>) => t.calls.filter((c) => c.method === "POST" && c.path === "/production/work-orders").length;
const openButton = () => screen.getByRole("button", { name: "開立工單" }) as HTMLButtonElement;

describe("圖紙頁開工單：結果不能確定時由共用的寫入追蹤器接手", () => {
  it.each([
    ["伺服器 500（已寫入後讀回失敗）", () => err(500, "INTERNAL", "伺服器錯誤")],
    ["連線中斷", () => Promise.reject(new TypeError("network down"))],
  ])("%s：提示可能已經開好、按鈕停用，再送也不會呼叫 API；核對後按「已核對」才能再開", async (_name, reply) => {
    const t = backend();
    t.on("POST", "/production/work-orders", reply as () => Response);
    show();
    await screen.findByRole("button", { name: "開立工單" });
    await submit();
    await waitFor(() => expect(document.body.textContent).toContain("上一次〈開立工單〉送出後結果未確認"));
    expect(openButton().disabled).toBe(true);
    expect(document.querySelector(".banner--unconfirmed")?.textContent).toContain("可能已經完成");
    // 停用的按鈕之外，直接送出表單也不會再呼叫 API
    await submit();
    expect(creates(t)).toBe(1);
    // 使用者到紀錄頁核對過，按「已核對」
    t.on("POST", "/production/work-orders", () => json(created));
    fireEvent.click(screen.getByLabelText("已核對〈開立工單〉"));
    await waitFor(() => expect(openButton().disabled).toBe(false));
    expect(document.querySelector(".banner--unconfirmed")).toBeNull();
    await submit();
    await waitFor(() => expect(document.body.textContent).toContain("WO-SYNTH-1"));
    expect(creates(t)).toBe(2);
  });

  it("結果未確認後重新載入頁面：一樣停用、一樣提示（瀏覽器裡只留動作名稱與零件編號）", async () => {
    const t = backend();
    t.on("POST", "/production/work-orders", () => err(503, "UNAVAILABLE"));
    show();
    await screen.findByRole("button", { name: "開立工單" });
    await submit();
    await waitFor(() => expect(openButton().disabled).toBe(true));
    const stored = Object.entries(localStorage).filter(([k]) => k.startsWith("artrag-writes-v1:"));
    expect(stored).toHaveLength(1);
    expect(stored[0][1]).toContain("work-order:create:mfg-002");
    expect(stored[0][1]).not.toMatch(/2026-10-20|"qty"|SYNTH/);
    // 重新載入：記憶體清空，從瀏覽器讀回
    cleanup();
    simulateReloadForTest();
    show();
    await screen.findByRole("button", { name: "開立工單" });
    expect(openButton().disabled).toBe(true);
    expect(document.body.textContent).toContain("上一次〈開立工單〉送出後結果未確認");
    await submit();
    expect(creates(t)).toBe(1);
  });

  it("送出中就離開頁面（回應還沒到）：重新載入後當成結果未確認，不會當成沒送出", async () => {
    const t = backend();
    t.on("POST", "/production/work-orders", () => new Promise<Response>(() => undefined));
    show();
    await screen.findByRole("button", { name: "開立工單" });
    await submit();
    expect(creates(t)).toBe(1);
    cleanup();
    simulateReloadForTest();
    show();
    await screen.findByRole("button", { name: "開立工單" });
    expect(openButton().disabled).toBe(true);
    expect(document.querySelector(".banner--unconfirmed")?.textContent).toContain("送出中頁面就關掉了");
  });

  it("超過額度改送主管核准：送核准回 500 → 同一個零件的開工單與送核准都不能再送（重新試算拿到新 pending_id 也一樣）", async () => {
    const t = backend();
    t.on("POST", "/production/work-orders", () => err(403, "APPROVAL_REQUIRED", "急件超過額度，需要主管核准"));
    let previews = 0;
    t.on("POST", "/changes/preview", () => json({ pending_id: `pend-${++previews}`, next: "approval", message: "需要主管核准" }));
    t.on("POST", /^\/changes\/pend-\d+\/request-approval$/, () => err(500, "INTERNAL"));
    show();
    await screen.findByRole("button", { name: "開立工單" });
    await submit();
    // 403 是伺服器明確拒絕：確定沒寫，照常給「送主管核准」
    const ask = await screen.findByRole("button", { name: "送主管核准" });
    expect(openButton().disabled).toBe(false);
    await act(async () => {
      fireEvent.click(ask);
    });
    await waitFor(() => expect(document.body.textContent).toContain("上一次〈送主管核准〉送出後結果未確認"));
    expect(openButton().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "送主管核准" })).toBeNull();
    await submit();
    expect(t.calls.filter((c) => /request-approval$/.test(c.path))).toHaveLength(1);
    expect(creates(t)).toBe(1);
  });

  it("伺服器明確拒絕（4xx）：確定沒寫，照常顯示錯誤，可以修正後再送", async () => {
    const t = backend();
    t.on("POST", "/production/work-orders", () => err(422, "VALIDATION", "交期不能早於排程起點"));
    show();
    await screen.findByRole("button", { name: "開立工單" });
    await submit();
    await waitFor(() => expect(document.body.textContent).toContain("交期不能早於排程起點"));
    expect(openButton().disabled).toBe(false);
    expect(document.querySelector(".banner--unconfirmed")).toBeNull();
    t.on("POST", "/production/work-orders", () => json(created));
    await submit();
    await waitFor(() => expect(document.body.textContent).toContain("WO-SYNTH-1"));
    expect(creates(t)).toBe(2);
  });
});
