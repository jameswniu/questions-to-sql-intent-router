import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The FastAPI app serves the build: index.html for / and /dashboard, and everything else under /static, which the
// sign-in proxy gate leaves open. The page's Content-Security-Policy allows scripts, styles and fonts from this origin
// only, so nothing is inlined: no data: fonts, no style tags, one stylesheet. The recorder for the README's demos
// appends its rules to /static/style.css, so that name stays fixed.
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
const proxied = ["/api", "/ask", "/session", "/feedback", "/evidence", "/healthz"];

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    assetsDir: "static",
    assetsInlineLimit: 0,
    cssCodeSplit: false,
    rolldownOptions: {
      output: {
        entryFileNames: "static/app.js",
        chunkFileNames: "static/[name]-[hash].js",
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css")) ? "static/style.css" : "static/[name]-[hash][extname]",
      },
    },
  },
  // npm run dev serves the app on :5173 and passes everything else to the API, run with make up or uvicorn.
  server: {
    proxy: Object.fromEntries(proxied.map((path) => [path, backend])),
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
