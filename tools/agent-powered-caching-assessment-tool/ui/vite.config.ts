import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Only the one variable this file reads; avoids pulling in @types/node.
declare const process: { env: Record<string, string | undefined> };

// The API has no authentication and answers loopback callers only. The dev and
// preview servers bind to 127.0.0.1 and forward /api, /health and /openapi.json to it,
// so the browser sees one origin and the API sees a loopback peer. Never widen the host.
const api = process.env.APCAT_API ?? "http://127.0.0.1:8000";
const proxy = {
  "/api": { target: api, changeOrigin: false },
  "/health": { target: api, changeOrigin: false },
  "/openapi.json": { target: api, changeOrigin: false },
};

export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 3000, strictPort: false, proxy },
  preview: { host: "127.0.0.1", port: 4173, proxy },
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 4096 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
