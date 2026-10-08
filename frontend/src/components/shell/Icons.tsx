// 來源：ArtRAG-前端demo/source/src/components/Icons.tsx（原樣移植）
const PATHS: Record<string, string> = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  newChat: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
  chevronDown: "m6 9 6 6 6-6",
  chevronRight: "m9 6 6 6-6 6",
  check: "M5 12.5 10 17 19 7",
  x: "M6 6l12 12M18 6 6 18",
  copy: "M9 9h10v10H9zM5 15V5h10",
  thumbUp: "M7 10v10H4V10zM7 10l4-7a2 2 0 0 1 3 2l-1 5h5.5a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 17.3 20H7",
  thumbDown: "M7 14V4H4v10zM7 14l4 7a2 2 0 0 0 3-2l-1-5h5.5a2 2 0 0 0 2-2.3l-1.2-6A2 2 0 0 0 17.3 4H7",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  sun: "M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
  image: "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15.5 9.5h.01",
  camera: "M4 8h3l2-3h6l2 3h3v11H4zM12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z",
  sparkle: "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4.3-4.3",
  frame: "M4 4h16v16H4zM4 15l4.5-4.5 4 4 2.5-2.5L20 17",
  ruler: "M3 17 17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2",
  box: "M3 7.5 12 3l9 4.5v9L12 21l-9-4.5zM3 7.5l9 4.5 9-4.5M12 12v9",
  cube: "M12 3 4 7.5v9L12 21l8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9",
  edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  calendar: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4",
  lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3",
  shield: "M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z",
  cpu: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
  arrowDown: "M12 5v14M5 12l7 7 7-7",
  wand: "M4 20 15 9M14 4v2M19 9h2M17.5 5.5 19 4M9 4l.8 1.7L11.5 6.5 9.8 7.3 9 9l-.8-1.7L6.5 6.5l1.7-.8z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  cloud: "M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 9z",
  home: "M4 10.5 12 4l8 6.5V20h-5v-6H9v6H4z",
  code: "m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14",
  download: "M12 4v12M6 11l6 6 6-6M5 20h14",
  table: "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
  layers: "M12 3 3 8l9 5 9-5zM3 13l9 5 9-5M3 17.5l9 5 9-5",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  menu: "M4 7h16M4 12h16M4 17h16",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  arrowLeft: "M19 12H5M12 5l-7 7 7 7",
  replay: "M4 12a8 8 0 1 0 2.3-5.7M4 4v5h5",
  minus: "M5 12h14",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  expand: "M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7",
  sidebar: "M4 5h16v14H4zM9.5 5v14",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  trash: "M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  chevronLeft: "m15 6-6 6 6 6",
  factory: "M3 20V10l6 3.5V10l6 3.5V5h6v15zM3 20h18",
  palette: "M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.8 1.6-1.6 0-1-.8-1.4-.8-2.4 0-.9.7-1.6 1.6-1.6H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3zM7.5 11.5h.01M10 7.5h.01M15 7.5h.01",
  arrowRight: "M5 12h14M12 5l7 7-7 7",
};

/** 線條圖示；尺寸與顏色交給主題（.icon） */
export function Icon({ name, className = "", strokeWidth = 1.6 }: { name: keyof typeof PATHS | string; className?: string; strokeWidth?: number }) {
  return (
    <svg viewBox="0 0 24 24" className={`icon ${className}`} fill="none" stroke="currentColor" strokeWidth={strokeWidth} aria-hidden>
      <path d={PATHS[name] ?? PATHS.sparkle} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 品牌標誌：「畫」字印記，外觀由主題決定 */
export function Mark({ className = "" }: { className?: string }) {
  return (
    <span className={`mark ${className}`} aria-hidden>
      畫
    </span>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return <span className={`spinner ${className}`} aria-hidden />;
}
