import { describe, expect, it } from "vitest";
import { appliedText, getSettings, setSetting, SETTINGS_KEY, withChatSettings } from "./settings";

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
});
