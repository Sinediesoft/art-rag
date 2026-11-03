/**
 * 藝術模組「影像對位與差異」用的幾何：/images/{id}/align 回傳照片四角（左上、右上、右下、左下）在原圖上的位置（0–1），
 * 前端用它把照片投影回原圖座標（拖曳比對），以及畫出四角連線、拍到的範圍。全部是同一組四角換算，不另外猜。
 */
export type Pt = [number, number];

/** 單位正方形 (0,0)(1,0)(1,1)(0,1) → 四邊形的透視轉換係數（Heckbert, Fundamentals of Texture Mapping, 1989） */
function squareToQuad(q: Pt[]) {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = q;
  const dx1 = x1 - x2;
  const dx2 = x3 - x2;
  const dx3 = x0 - x1 + x2 - x3;
  const dy1 = y1 - y2;
  const dy2 = y3 - y2;
  const dy3 = y0 - y1 + y2 - y3;
  if (Math.abs(dx3) < 1e-9 && Math.abs(dy3) < 1e-9) {
    return { a: x1 - x0, b: x3 - x0, c: x0, d: y1 - y0, e: y3 - y0, f: y0, g: 0, h: 0 };
  }
  const den = dx1 * dy2 - dx2 * dy1;
  const g = (dx3 * dy2 - dx2 * dy3) / den;
  const h = (dx1 * dy3 - dx3 * dy1) / den;
  return { a: x1 - x0 + g * x1, b: x3 - x0 + h * x3, c: x0, d: y1 - y0 + g * y1, e: y3 - y0 + h * y3, f: y0, g, h };
}

/**
 * CSS matrix3d：把 w×h 的元素（transform-origin: 0 0）的四角放到四邊形 quad（像素）。
 * 四邊形退化（三點共線、面積幾乎為 0）時回傳 null，不畫。
 */
export function quadTransform(w: number, h: number, quad: Pt[]): string | null {
  if (w <= 0 || h <= 0 || quad.length !== 4 || Math.abs(polygonArea(quad)) < 1) return null;
  const m = squareToQuad(quad);
  const v = [m.a / w, m.d / w, 0, m.g / w, m.b / h, m.e / h, 0, m.h / h, 0, 0, 1, 0, m.c, m.f, 0, 1];
  if (v.some((x) => !Number.isFinite(x))) return null;
  // 透視那兩項很小（約 1e-7）：保留有效位數，不能用 toFixed 四捨五入掉
  return `matrix3d(${v.map((x) => +x.toPrecision(12)).join(",")})`;
}

/** 多邊形面積（鞋帶公式；正負號看繞的方向） */
export function polygonArea(p: Pt[]): number {
  let s = 0;
  for (let i = 0; i < p.length; i++) {
    const [x1, y1] = p[i];
    const [x2, y2] = p[(i + 1) % p.length];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

/** 照片相對原圖轉了幾度：照片上緣（左上→右上）在原圖上的方向；要用原圖的長寬比換成真的角度 */
export function rotationOf(polygon: number[][], aspect: number): number {
  const [[x0, y0], [x1, y1]] = polygon;
  const deg = (Math.atan2((y1 - y0) / aspect, x1 - x0) * 180) / Math.PI;
  return Math.round(deg * 10) / 10;
}

export const scalePts = (pts: number[][], w: number, h: number): Pt[] => pts.map(([x, y]) => [x * w, y * h]);
export const ptsAttr = (pts: Pt[]) => pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
