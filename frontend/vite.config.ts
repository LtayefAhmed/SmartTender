import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API base is read at runtime from window.__SMARTTENDER_API__ (injected by
// nginx in the container) so one build works across environments. In dev we
// proxy /api and /health/etc straight to the backend on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
