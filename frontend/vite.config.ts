/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 開發時把 /api 轉到後端 8000，不用處理 CORS（共用層 §八）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
  // 元件與狀態測試（npm test）：jsdom＋可控制的 mock transport（src/test/transport.ts），不連後端、Jev、LLM 或雲端
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
