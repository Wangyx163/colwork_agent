import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  // Build straight into the directory the Python server serves. The output is
  // committed so a reviewer can clone and run the workbench with Python alone,
  // and CI never needs Node.
  build: {
    outDir: path.resolve(import.meta.dirname, "../src/collab_agent/static/console"),
    emptyOutDir: true,
    // Stable filenames: a hashed bundle would leave the previous build behind
    // in git on every rebuild, turning the committed output into landfill.
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "app-[name].js",
        assetFileNames: "app.[ext]",
      },
    },
  },
  // Assets live under one prefix shared by every page route, so the emitted
  // <script src> is absolute and resolves the same from /observatory and
  // /manage. Keep in step with ASSET_PREFIX in static_assets.py.
  base: "/console/",
});
