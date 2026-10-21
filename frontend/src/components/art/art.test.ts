import { describe, expect, it } from "vitest";
import { archive } from "../../shell/persist";
import { deriveOutputs } from "../../shell/outputs";
import type { ChatPart, Conv, Turn } from "../../shell/types";
import { done, route, sources } from "../../test/transport";
import { anchorOf, citesFor } from "./ArtDoc";
import { quadTransform, rotationOf, type Pt } from "./geometry";

/** matrix3d（行優先）套到一個點：和瀏覽器算 transform 的方式一樣 */
function apply(m: string, [x, y]: Pt): Pt {
  const a = m.replace(/^matrix3d\(|\)$/g, "").split(",").map(Number);
  const X = a[0] * x + a[4] * y + a[12];
  const Y = a[1] * x + a[5] * y + a[13];
  const W = a[3] * x + a[7] * y + a[15];
  return [X / W, Y / W];
}

describe("照片投影回原圖座標（拖曳比對）", () => {
  it("照片的四個角剛好落在對位回傳的四邊形上（透視）", () => {
    const quad: Pt[] = [
      [210, 120],
      [520, 95],
      [560, 380],
      [190, 330],
    ];
    const m = quadTransform(1200, 900, quad)!;
    const corners: Pt[] = [
      [0, 0],
      [1200, 0],
      [1200, 900],
      [0, 900],
    ];
    corners.forEach((c, i) => {
      const [x, y] = apply(m, c);
      expect(x).toBeCloseTo(quad[i][0], 3);
      expect(y).toBeCloseTo(quad[i][1], 3);
    });
  });

  it("平行四邊形（純仿射）也對", () => {
    const quad: Pt[] = [
      [10, 10],
      [110, 20],
      [100, 120],
      [0, 110],
    ];
    const m = quadTransform(400, 300, quad)!;
    expect(apply(m, [400, 300])[0]).toBeCloseTo(100, 6);
    expect(apply(m, [400, 300])[1]).toBeCloseTo(120, 6);
  });

  it("退化的四邊形（面積幾乎為 0）不畫", () => {
    expect(
      quadTransform(100, 100, [
        [0, 0],
        [10, 0],
        [20, 0],
        [30, 0],
      ]),
    ).toBeNull();
  });

  it("旋轉角度要換成原圖的長寬比才是真的角度", () => {
    const t = Math.tan((4 * Math.PI) / 180);
    // 原圖 4:3，照片上緣在像素上轉了 4°：寬方向走 0.5，高方向走 0.5 × tan4° × 4/3
    expect(rotationOf([[0.2, 0.3], [0.7, 0.3 + 0.5 * t * (4 / 3)]], 4 / 3)).toBe(4);
  });
});

describe("段落錨點與引用", () => {
  it("chunk_id 對到畫作資料的哪一段（backend/app/rag/chunking.py 的編號）", () => {
    expect(anchorOf("met-436535#03-1")).toBe("aw-p-3");
    expect(anchorOf("met-436535#meta")).toBe("aw-rec-meta");
    expect(anchorOf("met-436535#color")).toBe("aw-sec-color");
    expect(anchorOf("c1")).toBeNull();
  });

  const src = (ref: number, chunk: string, artwork = "met-436535") => ({ ref, chunk_id: chunk, level: "公開", artwork_id: artwork, title: "", topic: "", text: "", source_url: null, license: "CC0", score: 0.5 + ref / 10 });
  const chatTurn = (text: string, items: ReturnType<typeof src>[]): Turn => ({
    id: "t1",
    text: "",
    imageId: null,
    forced: null,
    at: "art",
    ts: 1,
    account: null,
    phase: "done",
    route: route(),
    failure: null,
    part: { kind: "chat", target: { artwork_id: "met-436535" }, status: "done", sources: sources("公開", { sources: items }) as unknown as ChatPart["sources"], text, done: done() as unknown as ChatPart["done"], error: null },
  });

  it("回答文字裡有 [n] 才算「回答引用」，其他只是檢索到；別幅畫的段落不算", () => {
    const t = chatTurn("前景是麥田 [1]。", [src(1, "met-436535#00-0"), src(2, "met-436535#02-0"), src(3, "met-437112#00-0", "met-437112")]);
    expect(citesFor("met-436535", [t])).toEqual({
      "met-436535#00-0": { ref: 1, score: 0.6, cited: true },
      "met-436535#02-0": { ref: 2, score: 0.7, cited: false },
    });
  });

  it("從紀錄還原的那一輪：用存下來的公開回答與段落", () => {
    const t = chatTurn("前景是麥田 [1]。", [src(1, "met-436535#00-0")]);
    const restored: Turn = { ...t, route: null, part: null, archived: archive(t) };
    expect(citesFor("met-436535", [restored])["met-436535#00-0"]?.cited).toBe(true);
  });
});

describe("照片辨識出的畫作：另成一份帶照片的成果", () => {
  const photoTurn = (photoId: string | null): Turn => ({
    id: "t1",
    text: "",
    imageId: "img-9",
    forced: null,
    at: "art",
    ts: 1,
    account: null,
    phase: "done",
    route: route({ question: "這是哪一幅？", photo: photoId ? { kind: "art", id: photoId, label: "有絲柏的麥田", domain: "art" } : null }),
    failure: null,
    part: null,
  });
  const conv = (t: Turn): Conv => ({ id: "c1", createdAt: 1, updatedAt: 1, turns: [t], active: {}, notices: [] });

  it("照片辨識出的就是這幅：成果帶照片編號（分頁標「× 你的照片」）", () => {
    const [o] = deriveOutputs(conv(photoTurn("met-436535"))).list;
    expect(o).toMatchObject({ key: "artwork:met-436535@img-9", kind: "artwork", artworkId: "met-436535", imageId: "img-9" });
  });

  it("附了照片但問的是別幅（或照片沒認出來）：一般的作品成果，不拿照片比對", () => {
    const [a] = deriveOutputs(conv(photoTurn("met-437112"))).list;
    expect(a.key).toBe("artwork:met-436535");
    expect(a.imageId).toBeUndefined();
    const [b] = deriveOutputs(conv({ ...photoTurn(null) })).list;
    expect(b.imageId).toBeUndefined();
  });

  it("重新整理後（從紀錄還原）照片比對還在：存檔只多一個布林，照片照舊只存編號", () => {
    const t = photoTurn("met-436535");
    const saved = archive(t);
    expect(saved.photoMatch).toBe(true);
    const restored: Turn = { ...t, route: null, part: null, archived: saved };
    expect(deriveOutputs(conv(restored)).list[0]).toMatchObject({ key: "artwork:met-436535@img-9", imageId: "img-9" });
    expect(archive(photoTurn("met-437112")).photoMatch).toBeUndefined();
  });
});
