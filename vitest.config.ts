import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// The web project loads the Lingui vite plugin, which finds its config from here when the run
// starts at the repo root.
process.env.LINGUI_CONFIG ??= fileURLToPath(new URL("./web/app/lingui.config.ts", import.meta.url));

const shared = {
  environment: "node",
  setupFiles: ["./vitest.setup.ts"],
  testTimeout: 30_000,
  hookTimeout: 60_000,
} as const;

export default defineConfig({
  test: {
    projects: [
      {
        extends: true,
        test: {
          ...shared,
          name: "workspace",
          include: [
            ".agents/skills/pr-watch/*.test.ts",
            "web/packages/*/src/**/*.test.{ts,tsx}",
            "host/share/deck-export/*.test.ts",
            "host/share/pptx/*.test.ts",
          ],
        },
      },
      {
        // The web tests run through the web app's own vite config, which carries the Lingui
        // macro transform; without it a test that does not mock `@lingui/*/macro` cannot load.
        extends: "./web/app/vite.config.ts",
        test: {
          ...shared,
          name: "web",
          include: ["web/app/src/**/*.test.{ts,tsx}"],
        },
      },
    ],
  },
});
