import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { acknowledgeWrite, blockedBy, simulateReloadForTest, trackWrite } from "./writes";

/** 架構審查 c7ca88c F1：結果不能確定的寫入由共用層負責到底，所有呼叫端同一套語意 */
const fail = (status: number) => Promise.reject(new ApiError(status ? `HTTP_${status}` : "NETWORK_ERROR", "x", "", status));
const stored = () => Object.entries(localStorage).filter(([k]) => k.startsWith("artrag-writes-v1:"));

describe("寫入追蹤：結果不能確定（5xx、斷線）的處理", () => {
  it.each([0, 500, 502, 503])("狀態 %i：留在瀏覽器、同一件事不能再送（不呼叫 API），按「已核對」後才能再送", async (status) => {
    const send = vi.fn(() => fail(status));
    await expect(trackWrite("核准", send, "approval:AP-1")).rejects.toBeInstanceOf(ApiError);
    expect(blockedBy("approval:AP-1")?.status).toBe("unknown");
    expect(stored()).toHaveLength(1);
    const again = vi.fn(() => Promise.resolve("ok"));
    await expect(trackWrite("核准", again, "approval:AP-1")).rejects.toMatchObject({ code: "WRITE_UNCONFIRMED", status: 409 });
    expect(again).not.toHaveBeenCalled();
    // 別的事不受影響
    await expect(trackWrite("退回", () => Promise.resolve("ok"), "approval:AP-2")).resolves.toBe("ok");
    acknowledgeWrite(blockedBy("approval:AP-1")!.id);
    expect(stored()).toHaveLength(0);
    await expect(trackWrite("核准", again, "approval:AP-1")).resolves.toBe("ok");
  });

  it("伺服器明確拒絕（4xx）或完成：不留下，同一件事可以再送", async () => {
    await expect(trackWrite("取消工單", () => fail(409), "work-order:cancel:WO-1")).rejects.toBeInstanceOf(ApiError);
    expect(blockedBy("work-order:cancel:WO-1")).toBeUndefined();
    await expect(trackWrite("取消工單", () => Promise.resolve("ok"), "work-order:cancel:WO-1")).resolves.toBe("ok");
    expect(blockedBy("work-order:cancel:WO-1")).toBeUndefined();
    expect(stored()).toHaveLength(0);
  });

  it("送出中：同一件事不能再送；送出中頁面就關掉，重新載入後當成結果未確認", async () => {
    void trackWrite("照片建檔入庫", () => new Promise(() => undefined), "intake:d-1");
    await expect(trackWrite("照片建檔入庫", () => Promise.resolve("ok"), "intake:d-1")).rejects.toMatchObject({ code: "WRITE_UNCONFIRMED" });
    simulateReloadForTest();
    expect(blockedBy("intake:d-1")?.status).toBe("unknown");
    expect(blockedBy("intake:d-1")?.foreign).toBe(true);
  });

  it("另一個分頁留下的（storage 事件）：同一件事一樣擋下", async () => {
    localStorage.setItem("artrag-writes-v1:other-1", JSON.stringify({ label: "開立工單", scope: "work-order:create:mfg-002", status: "pending", ts: 1, tab: "other" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "artrag-writes-v1:other-1" }));
    const send = vi.fn(() => Promise.resolve("ok"));
    await expect(trackWrite("開立工單", send, "work-order:create:mfg-002")).rejects.toMatchObject({ code: "WRITE_UNCONFIRMED" });
    expect(send).not.toHaveBeenCalled();
  });

  it("記不下「送出中」（儲存空間不足）：不送出，以 4xx 拒絕（確定沒寫）", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    const send = vi.fn(() => Promise.resolve("ok"));
    await expect(trackWrite("核准", send, "approval:AP-9")).rejects.toMatchObject({ code: "WRITE_NOT_RECORDED", status: 400 });
    expect(send).not.toHaveBeenCalled();
  });

  it("瀏覽器裡只留動作名稱、scope 與時間", async () => {
    await expect(trackWrite("核准", () => fail(500), "approval:AP-3")).rejects.toBeInstanceOf(ApiError);
    const [, value] = stored()[0];
    expect(Object.keys(JSON.parse(value)).sort()).toEqual(["label", "scope", "status", "tab", "ts"]);
  });
});
