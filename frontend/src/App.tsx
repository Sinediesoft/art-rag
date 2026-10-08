import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { useAccounts } from "./api/hooks";
import { Warp } from "./components/shell/Jump";
import { AdminPage } from "./pages/AdminPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { ArtworkPage } from "./pages/ArtworkPage";
import { BatchPage } from "./pages/BatchPage";
import { ChatPage } from "./pages/ChatPage";
import { ComparePage } from "./pages/ComparePage";
import { IntakePage } from "./pages/IntakePage";
import { InventoryPage } from "./pages/InventoryPage";
import { ItemComparePage } from "./pages/ItemComparePage";
import { PhotoDiffPage } from "./pages/PhotoDiffPage";
import { SchedulePage } from "./pages/SchedulePage";
import { SearchPage } from "./pages/SearchPage";
import { DrawingSearchPage } from "./pages/drawings/DrawingSearchPage";
import { PartChatPage } from "./pages/drawings/PartChatPage";
import { PartPage } from "./pages/drawings/PartPage";
import { ReconstructPage } from "./pages/drawings/ReconstructPage";
import { viewOfPath, type Domain } from "./shell/design";
import { reducedMotion } from "./shell/hooks";
import { deriveOutputs } from "./shell/outputs";
import { useShell } from "./shell/store";
import { applyStyles } from "./shell/theme";
import type { Conv } from "./shell/types";
import { EntryView } from "./views/EntryView";
import { FeatureView } from "./views/FeatureView";
import { ModuleView } from "./views/ModuleView";

/**
 * 來源：ArtRAG-前端demo/source/src/App.tsx（入口／模組切換、轉場、成果自動放上展示區）。
 * 網址：/ 入口首頁、/c/<id> 入口裡的一段對話、/factory/<id>、/art/<id> 模組；
 * 原本的功能頁網址（/search、/artworks/*、/drawings/*、/reconstruct、/inventory、/schedule、/approvals、/compare、
 * /photo-diff、/batch、/compare-items、/admin）照舊直接開啟，套模組外觀。
 */
export default function App() {
  const shell = useShell();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const view = viewOfPath(pathname);
  const [warp, setWarp] = useState<Domain | null>(null);
  /** 最後一段開著的對話：從功能頁回去時用 */
  const lastConv = useRef<string | null>(null);
  const m = pathname.match(/^\/(c|factory|art)\/([\w-]+)/);
  if (m) lastConv.current = m[2];
  shell.setCurrentConv(m?.[2] ?? null);

  // 入口用 01 Quiet、模組用 10 Night、藝術模組再疊暖色：畫面一換就只掛該用的樣式
  useLayoutEffect(() => applyStyles(view, shell.theme), [view, shell.theme]);

  // 目前的身分交給 store：和之前不同（主動切換在 store 裡已經處理；這裡多了憑證過期改發訪客、別的分頁切換）時，
  // 中止舊身分的請求、清掉舊身分的快取、收起其他身分的非公開內容
  const { data: accounts } = useAccounts();
  useEffect(() => {
    const me = accounts?.current;
    // 切換途中回來的身分查詢可能是舊的：切換結果由切換事件交給 store，這裡等切換完成再對
    if (me && !shell.switching) shell.setAccount(me.id, me.label);
  }, [accounts, shell]);

  const conv = shell.convs.find((c) => c.id === m?.[2]) ?? null;
  useAutoActive(conv);

  const jump = (convId: string, domain: Domain, key?: string) => {
    shell.enterModule(convId, domain, key);
    if (reducedMotion()) return navigate(`/${domain}/${convId}`);
    setWarp(domain);
    window.setTimeout(() => navigate(`/${domain}/${convId}`), 380);
    window.setTimeout(() => setWarp(null), 1000);
  };

  const feature = <FeatureView backTo={lastConv.current ? routeOf(shell.convs.find((c) => c.id === lastConv.current)) : null} />;

  return (
    <>
      <Routes>
        <Route index element={<EntryView key="new" convId={null} onJump={jump} />} />
        <Route path="c/:id" element={<EntryRoute onJump={jump} />} />
        <Route path="factory/:id" element={<ModuleRoute domain="factory" onJump={jump} />} />
        <Route path="art/:id" element={<ModuleRoute domain="art" onJump={jump} />} />
        <Route path="assistant" element={<Navigate to="/" replace />} />
        <Route path="drawings" element={<Navigate to="/" replace />} />
        <Route element={feature}>
          <Route path="search" element={<SearchPage />} />
          <Route path="artworks/intake" element={<IntakePage domain="art" />} />
          <Route path="artworks/:id" element={<ArtworkPage />} />
          <Route path="artworks/:id/chat" element={<ChatPage />} />
          <Route path="drawings/search" element={<DrawingSearchPage />} />
          <Route path="drawings/intake" element={<IntakePage domain="mfg" />} />
          <Route path="drawings/:id" element={<PartPage />} />
          <Route path="drawings/:id/chat" element={<PartChatPage />} />
          <Route path="drawings/:id/reconstruct" element={<ReconstructPage />} />
          <Route path="reconstruct" element={<ReconstructPage />} />
          <Route path="inventory" element={<InventoryPage />} />
          <Route path="schedule" element={<SchedulePage />} />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="compare" element={<ComparePage />} />
          <Route path="photo-diff" element={<PhotoDiffPage />} />
          <Route path="batch" element={<BatchPage />} />
          <Route path="compare-items" element={<ItemComparePage />} />
          <Route path="admin" element={<AdminPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Warp domain={warp} />
    </>
  );
}

const routeOf = (c: Conv | undefined) => (!c ? null : c.module ? `/${c.module}/${c.id}` : `/c/${c.id}`);

function EntryRoute({ onJump }: { onJump: (convId: string, domain: Domain, key?: string) => void }) {
  const { id } = useParams();
  const shell = useShell();
  if (!shell.convs.some((c) => c.id === id)) return <Navigate to="/" replace />;
  return <EntryView key={id} convId={id!} onJump={onJump} />;
}

function ModuleRoute({ domain, onJump }: { domain: Domain; onJump: (convId: string, domain: Domain, key?: string) => void }) {
  const { id } = useParams();
  const shell = useShell();
  const conv = shell.convs.find((c) => c.id === id);
  // 開啟紀錄、重新整理：記住最後進過的模組
  useEffect(() => {
    if (conv && conv.module !== domain) shell.enterModule(conv.id, domain);
  }, [conv, domain, shell]);
  if (!conv) return <Navigate to="/" replace />;
  return <ModuleView key={`${domain}:${conv.id}`} conv={conv} domain={domain} onJump={onJump} />;
}

/**
 * 新成果完成時自動成為展示區目前的成果（3D 轉好、排程算完、查詢回來）。
 * 剛打開一段紀錄時不動：保留上次看到的那一張。
 */
function useAutoActive(conv: Conv | null) {
  const shell = useShell();
  const prev = useRef<{ id: string | null; keys: Set<string> }>({ id: null, keys: new Set() });
  const outputs = deriveOutputs(conv);
  const sig = outputs.list.map((o) => o.key).join("|");
  useEffect(() => {
    const keys = new Set(outputs.list.map((o) => o.key));
    if (conv && prev.current.id === conv.id) {
      for (const d of ["factory", "art"] as Domain[]) {
        const fresh = outputs.list.filter((o) => o.domain === d && !prev.current.keys.has(o.key)).at(-1);
        if (fresh) shell.setActive(conv.id, d, fresh.key);
      }
    }
    prev.current = { id: conv?.id ?? null, keys };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, conv?.id]);
}
