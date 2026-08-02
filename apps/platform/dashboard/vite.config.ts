import { defineConfig, loadEnv } from "vite";
import solidPlugin from "vite-plugin-solid";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "DITTO_");
  return {
    plugins: [solidPlugin()],
    server: {
      port: 8080,
      // Same-origin /api/v1 default works in dev by proxying to the local API
      // (make api-up). Override the target for preview QA without changing
      // browser CORS behavior.
      proxy: {
        "/api": env.DITTO_DASHBOARD_PROXY_TARGET || "http://localhost:8000",
      },
    },
    build: {
      target: "es2022",
      outDir: "dist",
    },
  };
});
