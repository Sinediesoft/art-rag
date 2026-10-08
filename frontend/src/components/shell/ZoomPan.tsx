import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icons";

// 來源：ArtRAG-前端demo/source/src/components/ZoomPan.tsx（原樣移植）
export interface Focus {
  /** 百分比座標 */
  x: number;
  y: number;
  /** 要看到的寬度占全圖的比例（0.2＝放大 5 倍） */
  zoom: number;
}

interface View {
  s: number;
  tx: number;
  ty: number;
}

/**
 * 大圖檢視：滾輪縮放（以游標為中心）、拖曳平移、雙擊放大／還原、按鈕縮放。
 * 子元素（標註點）以百分比座標放在圖上，跟著圖一起移動但不跟著變大。
 */
export function ZoomPan({
  src,
  alt,
  focus,
  paper,
  children,
  label,
}: {
  src: string;
  alt: string;
  focus?: Focus | null;
  /** 圖紙：白底 */
  paper?: boolean;
  children?: (scale: number) => ReactNode;
  label?: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [nat, setNat] = useState<{ w: number; h: number } | null>(null);
  const [v, setV] = useState<View>({ s: 1, tx: 0, ty: 0 });
  const [anim, setAnim] = useState(false);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // 換圖時還原
  useEffect(() => {
    setNat(null);
    setV({ s: 1, tx: 0, ty: 0 });
  }, [src]);

  const fit = nat && size.w ? Math.min(size.w / nat.w, size.h / nat.h) * 0.9 : 0;
  const bw = nat ? nat.w * fit : 0;
  const bh = nat ? nat.h * fit : 0;
  const ox = (size.w - bw) / 2;
  const oy = (size.h - bh) / 2;

  const go = useCallback((next: View, animate = true) => {
    setAnim(animate);
    setV(next);
  }, []);

  // 外部指定放大到某一處（細看標註）
  useEffect(() => {
    if (!nat || !size.w) return;
    if (!focus) {
      go({ s: 1, tx: 0, ty: 0 });
      return;
    }
    const s = Math.min(8, Math.max(1, 1 / focus.zoom));
    const u = (focus.x / 100) * bw;
    const w = (focus.y / 100) * bh;
    go({ s, tx: size.w / 2 - ox - u * s, ty: size.h / 2 - oy - w * s });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, nat, size.w, size.h]);

  const zoomAt = (cx: number, cy: number, s2: number, animate = false) => {
    const s = Math.min(8, Math.max(1, s2));
    const u = (cx - ox - v.tx) / v.s;
    const w = (cy - oy - v.ty) / v.s;
    go(s === 1 ? { s: 1, tx: 0, ty: 0 } : { s, tx: cx - ox - u * s, ty: cy - oy - w * s }, animate);
  };

  const onWheel = (e: React.WheelEvent) => {
    const r = box.current!.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, v.s * Math.exp(-e.deltaY * 0.0015));
  };

  // 滾輪縮放時不要捲動頁面
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const stop = (e: WheelEvent) => e.preventDefault();
    el.addEventListener("wheel", stop, { passive: false });
    return () => el.removeEventListener("wheel", stop);
  }, []);

  const onDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest("button")) return;
    drag.current = { x: e.clientX, y: e.clientY, tx: v.tx, ty: v.ty, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (v.s > 1) go({ s: v.s, tx: d.tx + dx, ty: d.ty + dy }, false);
  };
  const onUp = () => {
    drag.current = null;
  };
  const onDouble = (e: React.MouseEvent) => {
    const r = box.current!.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, v.s > 1.2 ? 1 : 2.5, true);
  };

  const center = (k: number) => zoomAt(size.w / 2, size.h / 2, v.s * k, true);

  return (
    <div
      ref={box}
      className={`zp${paper ? " zp--paper" : ""}${v.s > 1 ? " is-zoomed" : ""}`}
      onWheel={onWheel}
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      onDoubleClick={onDouble}
    >
      <div
        className={`zp__layer${anim ? " is-anim" : ""}`}
        style={{ left: ox, top: oy, width: bw, height: bh, transform: `translate(${v.tx}px, ${v.ty}px) scale(${v.s})` }}
      >
        <img
          key={src}
          src={src}
          alt={alt}
          draggable={false}
          className="zp__img"
          onLoad={(e) => setNat({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
        />
        {nat && children?.(v.s)}
      </div>
      {label}
      <div className="zp__tools" role="toolbar" aria-label="縮放">
        <button type="button" onClick={() => center(1 / 1.6)} title="縮小" disabled={v.s <= 1}>
          <Icon name="minus" strokeWidth={1.8} />
        </button>
        <span className="zp__pct num">{Math.round(v.s * 100)}%</span>
        <button type="button" onClick={() => center(1.6)} title="放大" disabled={v.s >= 8}>
          <Icon name="plus" strokeWidth={1.8} />
        </button>
        <button type="button" onClick={() => go({ s: 1, tx: 0, ty: 0 })} title="看全圖" disabled={v.s === 1 && !v.tx}>
          <Icon name="fit" strokeWidth={1.8} />
        </button>
      </div>
    </div>
  );
}
