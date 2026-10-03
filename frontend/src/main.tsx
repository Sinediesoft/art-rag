import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./Layout";
import "./index.css";
import { AdminPage } from "./pages/AdminPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { ArtworkPage } from "./pages/ArtworkPage";
import { ChatPage } from "./pages/ChatPage";
import { ComparePage } from "./pages/ComparePage";
import { IntakePage } from "./pages/IntakePage";
import { InventoryPage } from "./pages/InventoryPage";
import { PhotoDiffPage } from "./pages/PhotoDiffPage";
import { SchedulePage } from "./pages/SchedulePage";
import { SearchPage } from "./pages/SearchPage";
import { DrawingSearchPage } from "./pages/drawings/DrawingSearchPage";
import { PartChatPage } from "./pages/drawings/PartChatPage";
import { PartPage } from "./pages/drawings/PartPage";
import { ReconstructPage } from "./pages/drawings/ReconstructPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          {/* 統一入口：首頁就是智慧助理（Layout 一直掛著它，切到下面的功能頁再回來，對話還在）。
              功能頁不放在導覽列，只從對話裡的「深入」按鈕進來。 */}
          <Route element={<Layout />}>
            <Route index element={null} />
            <Route path="assistant" element={<Navigate to="/" replace />} />
            <Route path="search" element={<SearchPage />} />
            <Route path="artworks/intake" element={<IntakePage domain="art" />} />
            <Route path="artworks/:id" element={<ArtworkPage />} />
            <Route path="artworks/:id/chat" element={<ChatPage />} />
            <Route path="drawings" element={<Navigate to="/" replace />} />
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
            <Route path="admin" element={<AdminPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
