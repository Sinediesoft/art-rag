import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { assetUrl } from "../api/client";

export interface ModelLayer {
  url: string;
  color: string;
  /** 半透明＋只畫輪廓，用來疊合比較 */
  ghost?: boolean;
}

/** STL 3D 檢視器：CAD 慣例 Z 軸朝上、可拖曳旋轉與縮放；多個模型以各自的外框中心對齊後疊合 */
export function ModelViewer({ layers, height = 360 }: { layers: ModelLayer[]; height?: number }) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const key = layers.map((l) => `${l.url}|${l.ghost}`).join(",");

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount || !layers.length) return;
    let disposed = false;
    setError(null);
    setLoading(true);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(mount.clientWidth, height);
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(1.5, -2, 3);
    scene.add(sun);

    const camera = new THREE.PerspectiveCamera(35, mount.clientWidth / height, 0.1, 1e5);
    camera.up.set(0, 0, 1);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.autoRotate = true;
    controls.autoRotateSpeed = 1.2;
    controls.addEventListener("start", () => (controls.autoRotate = false));

    let frame = 0;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
    };

    const loader = new STLLoader();
    Promise.all(layers.map((l) => loader.loadAsync(assetUrl(l.url))))
      .then((geoms) => {
        if (disposed) return;
        let radius = 1;
        geoms.forEach((g, i) => {
          const layer = layers[i];
          g.computeBoundingBox();
          const center = g.boundingBox!.getCenter(new THREE.Vector3());
          g.translate(-center.x, -center.y, -center.z);
          g.computeBoundingSphere();
          radius = Math.max(radius, g.boundingSphere!.radius);
          const mat = new THREE.MeshStandardMaterial({
            color: layer.color,
            metalness: 0.25,
            roughness: 0.55,
            transparent: !!layer.ghost,
            opacity: layer.ghost ? 0.18 : 1,
            depthWrite: !layer.ghost,
            polygonOffset: true,
            polygonOffsetFactor: 1,
          });
          scene.add(new THREE.Mesh(g, mat));
          const edges = new THREE.LineSegments(
            new THREE.EdgesGeometry(g, 25),
            new THREE.LineBasicMaterial({ color: layer.ghost ? layer.color : 0x1f2933 }),
          );
          scene.add(edges);
        });
        const grid = new THREE.GridHelper(radius * 4, 20, 0xc9ccd1, 0xe3e5e8);
        grid.rotation.x = Math.PI / 2;
        grid.position.z = -radius * 0.75;
        scene.add(grid);
        camera.position.set(radius * 2.2, -radius * 2.6, radius * 1.8);
        camera.near = radius / 100;
        camera.far = radius * 100;
        camera.updateProjectionMatrix();
        controls.target.set(0, 0, 0);
        setLoading(false);
        tick();
      })
      .catch(() => {
        if (!disposed) {
          setError("3D 模型載入失敗");
          setLoading(false);
        }
      });

    const onResize = () => {
      camera.aspect = mount.clientWidth / height;
      camera.updateProjectionMatrix();
      renderer.setSize(mount.clientWidth, height);
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(mount);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      scene.traverse((o) => {
        const m = o as THREE.Mesh;
        m.geometry?.dispose();
        (Array.isArray(m.material) ? m.material : [m.material]).forEach((x) => x?.dispose());
      });
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, height]);

  return (
    <div className="relative overflow-hidden rounded-xl border border-line bg-gradient-to-b from-white to-steel-soft/60">
      <div ref={mountRef} style={{ height }} className="w-full cursor-grab active:cursor-grabbing" />
      {loading && !error && (
        <div className="absolute inset-0 grid place-items-center text-sm text-ink-faint">載入 3D 模型…</div>
      )}
      {error && <div className="absolute inset-0 grid place-items-center text-sm text-seal">{error}</div>}
      <p className="pointer-events-none absolute bottom-2 right-3 text-[11px] text-ink-faint">拖曳旋轉 · 滾輪縮放</p>
    </div>
  );
}
