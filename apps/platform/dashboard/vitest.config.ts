import { defineConfig } from "vitest/config";
import solidPlugin from "vite-plugin-solid";

// Standalone test config (not merged with vite.config.ts): tests never need
// the dev proxy, and Solid under vitest wants the browser resolve conditions
// so the reactive runtime — not the SSR stub — is loaded into jsdom.
export default defineConfig({
  plugins: [solidPlugin()],
  resolve: {
    conditions: ["development", "browser"],
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
