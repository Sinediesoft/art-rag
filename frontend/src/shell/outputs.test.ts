import { describe, expect, it } from "vitest";
import { done, factoryRoute, sources } from "../test/transport";
import { ctaOf, deriveOutputs, domainOf, foundDrawing } from "./outputs";
import type { ChatPart, Conv, Turn } from "./types";

/**
 * 圖紙找到了、問題答不出來（TASK-20261010-001）：第 1 段確認看得到、第 6 段降級。
 * 模組右側保留這張圖紙（不當成圖紙不存在）；看不到或不存在的圖紙不會走到這裡。
 */
const stage = (n: number, status: "pass" | "block") => ({ stage: n as 1, name: `第 ${n} 段`, status, by: "地端", detail: "" });
const PIPE_GATE_BLOCK = [1, 2, 3, 4, 5].map((n) => stage(n, "pass")).concat(stage(6, "block"));

function chatTurn(over: { pipeline?: ReturnType<typeof stage>[]; docLevel?: string | null; degraded?: boolean; partId?: string } = {}): Turn {
  const partId = over.partId ?? "mfg-006";
  const route = factoryRoute({
    question: "",
    photo: { kind: "drawing", id: "mfg-006", label: "立式軸承座", domain: "mfg" },
    dispatch: { module: "drawing_qa", artwork_id: null, part_id: "mfg-006", part_label: "立式軸承座", question: "這張圖紙的重點是什麼？" },
    auth: {
      ...factoryRoute().auth,
      filter: { domain: "mfg", domain_label: "工廠圖紙", clearance: 2, depts: ["生管"], levels: ["公開", "內部", "機密"], doc_id: "mfg-006", doc_label: "立式軸承座", doc_level: over.docLevel === undefined ? "機密" : over.docLevel, text: "" },
    },
  });
  const degraded = over.degraded ?? true;
  const src = sources("機密", { part_id: partId, artwork_id: null, sources: [], pipeline: over.pipeline ?? PIPE_GATE_BLOCK });
  src.post_filter.gate = { ...src.post_filter.gate, passed: !degraded, message: degraded ? "查無資料" : null };
  const part: ChatPart = {
    kind: "chat",
    target: { part_id: partId },
    status: "done",
    sources: src as unknown as ChatPart["sources"],
    text: degraded ? "查無資料" : "立式軸承座……",
    done: done({ degraded, pipeline: over.pipeline ?? PIPE_GATE_BLOCK }) as unknown as ChatPart["done"],
    error: null,
  };
  return { id: "t1", text: "", imageId: "img-1", forced: null, at: "entry", ts: 1, account: null, phase: "done", route, failure: null, part };
}

const conv = (t: Turn): Conv => ({ id: "c1", createdAt: 1, updatedAt: 1, turns: [t], active: {}, notices: [] });

describe("找到圖紙但問題答不出來", () => {
  it("第 1 段通過、第 6 段降級：保留圖紙成果，不給跳轉（ADR 016 降級不給按鈕）", () => {
    const t = chatTurn();
    expect(foundDrawing(t)).toBe(true);
    expect(domainOf(t)).toBeNull();
    expect(ctaOf(t)).toBeNull();
    const { list } = deriveOutputs(conv(t));
    expect(list.map((o) => [o.kind, o.partId, o.title])).toEqual([["drawing", "mfg-006", "立式軸承座"]]);
  });

  it("問答成功：一般的工廠成果（第 1 段確認的那一張）", () => {
    const t = chatTurn({ degraded: false, pipeline: [1, 2, 3, 4, 5, 6, 7].map((n) => stage(n, "pass")) });
    expect(foundDrawing(t)).toBe(false);
    expect(domainOf(t)).toBe("factory");
    expect(deriveOutputs(conv(t)).list.map((o) => o.partId)).toEqual(["mfg-006"]);
  });

  it("第 7 段輸出檢查擋下：圖紙同樣是第 1 段確認看得到的，保留圖紙（回答沒送出，不給跳轉）", () => {
    const t = chatTurn({ pipeline: [1, 2, 3, 4, 5, 6].map((n) => stage(n, "pass")).concat(stage(7, "block")) });
    expect(foundDrawing(t)).toBe(true);
    expect(ctaOf(t)).toBeNull();
    expect(deriveOutputs(conv(t)).list.map((o) => o.partId)).toEqual(["mfg-006"]);
  });

  it("/chat 第 1 段擋下（看不到或不存在）：不留下任何成果", () => {
    const t = chatTurn({ pipeline: [stage(1, "block")] });
    expect(foundDrawing(t)).toBe(false);
    expect(deriveOutputs(conv(t)).list).toEqual([]);
  });

  it("/agent/route 沒有確認看得到（沒有 doc_level）：不留下任何成果", () => {
    const t = chatTurn({ docLevel: null });
    expect(foundDrawing(t)).toBe(false);
    expect(deriveOutputs(conv(t)).list).toEqual([]);
  });

  it("問答的對象和第 1 段確認的不是同一張：不留下", () => {
    const t = chatTurn({ partId: "mfg-002" });
    expect(foundDrawing(t)).toBe(false);
    expect(deriveOutputs(conv(t)).list).toEqual([]);
  });
});
