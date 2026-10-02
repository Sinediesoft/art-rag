import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./Layout";
import "./index.css";
import { AdminPage } from "./pages/AdminPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { ArtworkPage } from "./pages/ArtworkPage";
import { AssistantPage } from "./pages/AssistantPage";
import { ChatPage } from "./pages/ChatPage";
import { ComparePage } from "./pages/ComparePage";
import { HomePage } from "./pages/HomePage";
import { InventoryPage } from "./pages/InventoryPage";
import { PhotoDiffPage } from "./pages/PhotoDiffPage";
import { SchedulePage } from "./pages/SchedulePage";
import { SearchPage } from "./pages/SearchPage";
import { DrawingSearchPage } from "./pages/drawings/DrawingSearchPage";
import { DrawingsHomePage } from "./pages/drawings/DrawingsHomePage";
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
          <Route element={<Layout />}>
            <Route index element={<HomePage />} />
            <Route path="search" element={<SearchPage />} />
            <Route path="artworks/:id" element={<ArtworkPage />} />
            <Route path="artworks/:id/chat" element={<ChatPage />} />
            <Route path="drawings" element={<DrawingsHomePage />} />
            <Route path="drawings/search" element={<DrawingSearchPage />} />
            <Route path="drawings/:id" element={<PartPage />} />
            <Route path="drawings/:id/chat" element={<PartChatPage />} />
            <Route path="drawings/:id/reconstruct" element={<ReconstructPage />} />
            <Route path="reconstruct" element={<ReconstructPage />} />
            <Route path="inventory" element={<InventoryPage />} />
            <Route path="schedule" element={<SchedulePage />} />
            <Route path="assistant" element={<AssistantPage />} />
            <Route path="approvals" element={<ApprovalsPage />} />
            <Route path="compare" element={<ComparePage />} />
            <Route path="photo-diff" element={<PhotoDiffPage />} />
            <Route path="admin" element={<AdminPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
