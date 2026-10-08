import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";
import { viewOfPath } from "./shell/design";
import { load } from "./shell/persist";
import { ShellProvider } from "./shell/store";
import { applyStyles } from "./shell/theme";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

// 第一次畫面出來前就依網址掛好入口／模組的樣式，避免閃一下
applyStyles(viewOfPath(location.pathname), load().prefs.theme);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ShellProvider>
          <App />
        </ShellProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
