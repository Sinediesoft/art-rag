"""3D 重建的評估指標：IoU（體積交聯比），兩種對齊方式。

- aligned_iou（論文評估法，可與論文數字比較）：照 Ortho2CAD／CAD-Coder 的評估腳本，兩個實體各自
  移到質心、以迴轉半徑正規化尺度，再用慣性主軸對齊（4 種翻轉取最大）。另外加一個「不旋轉」的
  候選：三視圖已固定座標軸，主軸在接近正方形的零件上會互換，只靠主軸對齊會低估。
  這個方法對薄壁件很嚴格：8 mm 壁厚的 L 型支架，壁位置偏幾個百分點就幾乎不重疊。
- bbox_iou（外框對齊）：把重建結果的外框對齊並縮放到標準模型的外框（＝圖紙標註的外形尺寸），
  看形狀本身像不像；工程上更直觀。

體積改用體素計算（每軸 64 格、沿 Z 軸射線奇偶判斷內外），不用 OCC 的布林運算：
模型產生的實體常有細微瑕疵，實測布林交集會默默回傳 0（兩塊幾乎相同的板子算出 IoU 0.09），
體素法不受影響；與精確值的誤差約 3%。
"""

import numpy as np


def mesh(shape) -> list[np.ndarray]:
    """每個實體各自三角化成 (T, 3, 3)；重疊的多個實體分開算內外，最後取聯集。"""
    bb = shape.BoundingBox()
    tol = max(bb.xlen, bb.ylen, bb.zlen) * 2e-3
    out = []
    for s in shape.Solids() or [shape]:
        verts, tris = s.tessellate(tol, 0.2)
        if tris:
            v = np.array([(p.x, p.y, p.z) for p in verts])
            out.append(v[np.array(tris)])
    return out


def voxelize(tri_sets: list[np.ndarray], lo: np.ndarray, hi: np.ndarray, n: int = 64) -> np.ndarray:
    step = (hi - lo) / n
    # 射線位置加一點不規則偏移，避免剛好穿過相鄰三角形的共用邊而重複計數
    xs = lo[0] + (np.arange(n) + 0.5 + 1.3e-3) * step[0]
    ys = lo[1] + (np.arange(n) + 0.5 + 2.7e-3) * step[1]
    occ = np.zeros((n, n, n), bool)
    for tris in tri_sets:
        hits = np.zeros((n, n, n + 1), np.int32)
        a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
        det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        keep = np.abs(det) > 1e-12
        for ta, tb, tc, d in zip(a[keep], b[keep], c[keep], det[keep], strict=True):
            i0 = np.searchsorted(xs, min(ta[0], tb[0], tc[0]))
            i1 = np.searchsorted(xs, max(ta[0], tb[0], tc[0]))
            j0 = np.searchsorted(ys, min(ta[1], tb[1], tc[1]))
            j1 = np.searchsorted(ys, max(ta[1], tb[1], tc[1]))
            if i0 >= i1 or j0 >= j1:
                continue
            x, y = np.meshgrid(xs[i0:i1], ys[j0:j1], indexing="ij")
            l1 = ((tb[1] - tc[1]) * (x - tc[0]) + (tc[0] - tb[0]) * (y - tc[1])) / d
            l2 = ((tc[1] - ta[1]) * (x - tc[0]) + (ta[0] - tc[0]) * (y - tc[1])) / d
            l3 = 1 - l1 - l2
            inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            if not inside.any():
                continue
            z = l1 * ta[2] + l2 * tb[2] + l3 * tc[2]
            k = np.clip(np.ceil((z - lo[2]) / step[2] - 0.5), 0, n).astype(int)
            ii, jj = np.nonzero(inside)
            np.add.at(hits, (ii + i0, jj + j0, k[ii, jj]), 1)
        occ |= (np.cumsum(hits, axis=2)[:, :, :n] % 2) == 1
    return occ


def voxel_iou(a: list[np.ndarray], b: list[np.ndarray], n: int = 64) -> float:
    pts = np.concatenate([t.reshape(-1, 3) for t in a + b])
    lo, hi = pts.min(0), pts.max(0)
    pad = (hi - lo) * 0.01 + 1e-9
    va, vb = voxelize(a, lo - pad, hi + pad, n), voxelize(b, lo - pad, hi + pad, n)
    union = (va | vb).sum()
    return float((va & vb).sum() / union) if union else 0.0


def bbox_iou(source, target) -> float:
    """外框對齊 IoU：各軸分別縮放，讓重建結果的外框與標準模型重合後再算 IoU。"""
    sb, tb = source.BoundingBox(), target.BoundingBox()
    s_lo, s_sz = np.array([sb.xmin, sb.ymin, sb.zmin]), np.array([sb.xlen, sb.ylen, sb.zlen])
    t_lo, t_sz = np.array([tb.xmin, tb.ymin, tb.zmin]), np.array([tb.xlen, tb.ylen, tb.zlen])
    if (s_sz <= 0).any():
        return 0.0
    fitted = [(m - s_lo) / s_sz * t_sz + t_lo for m in mesh(source)]
    return voxel_iou(fitted, mesh(target))


def aligned_iou(source, target) -> float:
    """source／target：cq.Shape。回傳 0–1。"""
    import cadquery as cq

    v_s, v_t = source.Volume(), target.Volume()
    if v_s <= 0 or v_t <= 0:
        return 0.0
    c_s, c_t = source.Center().toTuple(), target.Center().toTuple()
    p_s, vec_s = np.linalg.eigh(np.array(cq.Shape.matrixOfInertia(source)))
    p_t, vec_t = np.linalg.eigh(np.array(cq.Shape.matrixOfInertia(target)))
    s_s = np.sqrt(np.abs(p_s).sum() / v_s)
    s_t = np.sqrt(np.abs(p_t).sum() / v_t)
    ms = [(t - np.array(c_s)) / s_s for t in mesh(source)]
    mt = [(t - np.array(c_t)) / s_t for t in mesh(target)]
    rots = [np.eye(3), vec_t @ vec_s.T]
    for i in range(3):
        flip = 1 - 2 * np.array([i > 0, (i + 1) % 2, i % 3 <= 1])
        rots.append(vec_t @ (flip[None, :] * vec_s).T)
    return max(voxel_iou([t @ r.T for t in ms], mt) for r in rots)
