import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8766",
    viewport: { width: 1440, height: 1100 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: ".venv/bin/python tests/browser/server.py",
    url: "http://127.0.0.1:8766/api/health",
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
