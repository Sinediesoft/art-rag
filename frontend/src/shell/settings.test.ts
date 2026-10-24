import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { appliedText, getSettings, setSetting, SETTINGS_KEY, useSettings, withChatSettings } from "./settings";

const otherTab = (key: string | null) => act(() => { window.dispatchEvent(new StorageEvent("storage", { key })); });

describe("設定面板的問答開關（瀏覽器儲存）", () => {
  it("預設全部「依伺服器預設」：請求不帶這三個欄位", () => {
    expect(getSettings()).toEqual({ rearrange: "server", conflict_check: "server", send_image: "server" });
    const { body, applied } = withChatSettings({ question: "q", artwork_id: "npm-000001" });
    expect(body).toEqual({ question: "q", artwork_id: "npm-000001" });
    expect(applied).toEqual({});
    expect(appliedText(applied)).toBeNull();
  });

  it("設成開／關就存進瀏覽器並帶進請求，回傳實際套用的", () => {
    setSetting("rearrange", "off");
    setSetting("send_image", "on");
    expect(JSON.parse(localStorage.getItem(SETTINGS_KEY)!)).toEqual({ rearrange: "off", conflict_check: "server", send_image: "on" });
    const { body, applied } = withChatSettings({ question: "q" });
    expect(body).toEqual({ question: "q", rearrange: false, send_image: true });
    expect(applied).toEqual({ rearrange: false, send_image: true });
    expect(appliedText(applied)).toBe("依你的設定：段落篩選 關・回答附圖 開");
  });

  it("請求自己指定的欄位不套用（策略比較頁固定用伺服器預設）", () => {
    setSetting("rearrange", "off");
    setSetting("conflict_check", "off");
    const { body, applied } = withChatSettings({ question: "q", rearrange: null, conflict_check: true });
    expect(body).toEqual({ question: "q", rearrange: null, conflict_check: true });
    expect(applied).toEqual({});
  });

  it("內容沒變時回傳同一個物件；壞掉或不認得的值回到預設", () => {
    expect(getSettings()).toBe(getSettings());
    localStorage.setItem(SETTINGS_KEY, "{不是 JSON");
    expect(getSettings().rearrange).toBe("server");
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ rearrange: "maybe", conflict_check: "on", engine: "local" }));
    expect(getSettings()).toEqual({ rearrange: "server", conflict_check: "on", send_image: "server" });
  });

  it("其他分頁改了設定或清除網站資料時跟著更新，別的 key 不理會", () => {
    const { result } = renderHook(() => useSettings());
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ rearrange: "on" }));
    otherTab("artrag-shell-v1");
    expect(result.current.rearrange).toBe("server");
    otherTab(SETTINGS_KEY);
    expect(result.current.rearrange).toBe("on");
    localStorage.clear();
    otherTab(null);
    expect(result.current.rearrange).toBe("server");
  });

  it("多個元件共用一個 storage 監聽，最後一個卸載才移除", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const a = renderHook(() => useSettings());
    const b = renderHook(() => useSettings());
    expect(add.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
    a.unmount();
    expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(0);
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ send_image: "off" }));
    otherTab(SETTINGS_KEY);
    expect(b.result.current.send_image).toBe("off");
    b.unmount();
    expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
    add.mockRestore();
    remove.mockRestore();
  });

  // 放最後：存不了時記在模組內的 memory，之後的測試會讀到它
  it("瀏覽器存不了（無痕、封鎖網站資料）時這個分頁照樣生效", () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    setSetting("conflict_check", "off");
    expect(getSettings().conflict_check).toBe("off");
    expect(withChatSettings({ question: "q" }).body).toEqual({ question: "q", conflict_check: false });
    get.mockRestore();
    set.mockRestore();
  });
});
