import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API base for dev:
//   VITE_API_BASE=http://127.0.0.1:8000 npm run dev
// In production the same env var is baked at build time.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
