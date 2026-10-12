import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5178,
    strictPort: true,
    watch: {
      ignored: [
        "**/src-tauri/**",
        "**/engine/**",
        "**/.platform-tests/**",
        "**/.ember-wine-probe-*/**",
        "**/.engine-build/**",
        "**/release/**",
      ],
    },
    proxy: {
      "/api": {
        target: process.env.EMBER_PREVIEW_BACKEND || "http://127.0.0.1:5179",
        rewrite: (p) =>
          p.replace(
            "/api",
            "/" + (process.env.EMBER_PREVIEW_KEY || "unconfigured"),
          ),
      },
    },
  },
  clearScreen: false,
});
