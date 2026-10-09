import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps asset and sample-report URLs relative, so the build works
// from any mount point (FastAPI static mount, subpath, file preview).
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
  preview: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
