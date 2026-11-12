import path from "node:path";
import { lingui } from "@lingui/vite-plugin";
import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { apiProxy } from "./src/api-proxy.js";

const webPort = Number(process.env.WEB_PORT ?? 5173);

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, path.resolve(import.meta.dirname, "../.."), "");
  const engine =
    process.env.OMNIGENT_URL ??
    rootEnv.OMNIGENT_URL ??
    process.env.ENGINE_URL ??
    rootEnv.ENGINE_URL ??
    "http://127.0.0.1:8012";
  const previewHost = process.env.SUPERCHAT_HOST ?? rootEnv.SUPERCHAT_HOST ?? "localhost";
  const performanceAssetDelayMs = Number(process.env.SUPERCHAT_PERFORMANCE_ASSET_DELAY_MS ?? 0);
  return {
    plugins: [
      react(),
      babel({ plugins: ["@lingui/babel-plugin-lingui-macro"] }),
      lingui(),
      tailwindcss(),
      {
        name: "superchat-performance-asset-delay",
        configurePreviewServer(server) {
          if (!Number.isFinite(performanceAssetDelayMs) || performanceAssetDelayMs <= 0) return;
          server.middlewares.use((req, _res, next) => {
            const pathname = req.url?.split("?", 1)[0] ?? "/";
            if (["/v1", "/apps/"].some((prefix) => pathname.startsWith(prefix))) {
              next();
              return;
            }
            setTimeout(next, performanceAssetDelayMs);
          });
        },
      },
    ],
    server: {
      host: "127.0.0.1",
      port: webPort,
      strictPort: true,
      proxy: {
        // The engine's REST API: the app talks to it directly, on this origin.
        "/v1": apiProxy(engine),
      },
    },
    preview: {
      host: "0.0.0.0",
      port: Number(process.env.WEB_PORT ?? 5173),
      allowedHosts: [previewHost],
      proxy: {
        // The engine's REST API: the app talks to it directly, on this origin.
        "/v1": apiProxy(engine),
      },
    },
  };
});
