import { lazy, Suspense, type ComponentProps } from "react";

// three.js 約 600 KB：只在需要 3D 檢視時才載入，首頁與畫作頁不受影響
const Viewer = lazy(() => import("./ModelViewer").then((m) => ({ default: m.ModelViewer })));

export type { ModelLayer } from "./ModelViewer";

export function ModelViewer(props: ComponentProps<typeof Viewer>) {
  return (
    <Suspense
      fallback={
        <div
          style={{ height: props.height ?? 360 }}
          className="grid place-items-center rounded-xl border border-hairline bg-canvas text-sm text-ink-48"
        >
          載入 3D 檢視器…
        </div>
      }
    >
      <Viewer {...props} />
    </Suspense>
  );
}
