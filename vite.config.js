import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";
import { fileURLToPath } from "node:url";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: "frontend",
  publicDir: "public",
  base: "/",
  build: {
    outDir: path.join(rootDir, "frontend/dist"),
    emptyOutDir: true,
  },
  plugins: [
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["apple-touch-icon.png", "icons/*.png"],
      filename: "sw.js",
      manifest: {
        name: "Studio",
        short_name: "Studio",
        description: "인스타 카드뉴스·릴스 자동 제작 (top.pick00)",
        lang: "ko",
        theme_color: "#2b5bff",
        background_color: "#eef3ff",
        display: "standalone",
        orientation: "portrait",
        start_url: "/",
        scope: "/",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "icons/icon-512-maskable.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,ico,png,svg,webp}"],
        navigateFallback: "index.html",
        navigateFallbackDenylist: [/^\/api/, /^\/media/, /^\/icons/, /sw\.js$/, /workbox/],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/api/"),
            handler: "NetworkOnly",
            method: "GET",
          },
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/api/"),
            handler: "NetworkOnly",
            method: "POST",
          },
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/media/"),
            handler: "StaleWhileRevalidate",
            options: {
              cacheName: "studio-media",
              expiration: { maxEntries: 80, maxAgeSeconds: 60 * 60 * 24 * 7 },
            },
          },
        ],
      },
      devOptions: { enabled: true, type: "module" },
    }),
  ],
  server: {
    host: true,
    port: 5173,
    open: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/media": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
  preview: {
    host: true,
    port: 5173,
  },
});
