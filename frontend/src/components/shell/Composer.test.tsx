import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY } from "../../shell/design";
import { json, mockTransport } from "../../test/transport";
import { Composer } from "./Composer";

vi.mock("../../lib/image", () => ({ preprocessImage: async (f: File) => f }));

const setup = (busy = false) => {
  const onSend = vi.fn();
  const onStop = vi.fn();
  render(<Composer busy={busy} onSend={onSend} onStop={onStop} copy={COPY.entry} />);
  return { onSend, onStop, input: screen.getByLabelText("輸入問題") as HTMLTextAreaElement };
};

describe("Composer：送出、換行、IME、附件、停止", () => {
  it("Enter 送出並清空輸入框", () => {
    const { onSend, input } = setup();
    fireEvent.change(input, { target: { value: "連接法蘭有哪些公差要求？" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("連接法蘭有哪些公差要求？", null);
    expect(input.value).toBe("");
  });

  it("Shift+Enter 換行，不送出", () => {
    const { onSend, input } = setup();
    fireEvent.change(input, { target: { value: "第一行" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("IME 組字中（isComposing／keyCode 229）按 Enter 不送出", () => {
    const { onSend, input } = setup();
    fireEvent.change(input, { target: { value: "ㄈㄚˇ" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("空白不送出", () => {
    const { onSend, input } = setup();
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect((screen.getByLabelText("送出") as HTMLButtonElement).disabled).toBe(true);
  });

  it("附照片：先上傳到伺服器，預覽用伺服器網址（不是 blob），可以移除；只有照片也能送出", async () => {
    const t = mockTransport();
    t.on("POST", "/images", () => json({ image_id: "img-1" }));
    const { onSend } = setup();
    const file = new File(["x"], "photo.jpg", { type: "image/jpeg" });
    await act(async () => {
      fireEvent.change(screen.getByTestId("composer-file"), { target: { files: [file] } });
    });
    const img = await screen.findByAltText("附加的照片");
    expect(img.getAttribute("src")).toBe("/api/v1/images/img-1");
    expect(img.getAttribute("src")).not.toMatch(/^blob:/);
    fireEvent.click(screen.getByLabelText("移除照片"));
    expect(screen.queryByAltText("附加的照片")).toBeNull();
    await act(async () => {
      fireEvent.change(screen.getByTestId("composer-file"), { target: { files: [file] } });
    });
    await screen.findByAltText("附加的照片");
    fireEvent.click(screen.getByLabelText("送出"));
    expect(onSend).toHaveBeenCalledWith("", "img-1");
  });

  it("照片上傳失敗顯示錯誤", async () => {
    const t = mockTransport();
    t.on("POST", "/images", () => json({ error: { code: "IMAGE_TOO_LARGE", message: "照片太大", request_id: "r1" } }, 413));
    setup();
    await act(async () => {
      fireEvent.change(screen.getByTestId("composer-file"), { target: { files: [new File(["x"], "a.jpg")] } });
    });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("照片太大"));
  });

  it("回答中：送出鈕變成停止，按了呼叫 onStop；Enter 不會再送", () => {
    const { onSend, onStop, input } = setup(true);
    fireEvent.change(input, { target: { value: "再問一句" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("停止"));
    expect(onStop).toHaveBeenCalled();
  });
});
