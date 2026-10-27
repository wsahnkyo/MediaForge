import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/static/",
  build: {
    outDir: "../src/mediaforge/web",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
