import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.VIEWER_TEST_PORT ?? 4174);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid Viewer fixture port");

export default defineConfig({
  testDir: "./tests", testMatch: "remote-viewer*.spec.ts", fullyParallel: false,
  outputDir: "./test-results/viewer",
  workers: 1, retries: 0, reporter: "line", use: { trace: "retain-on-failure" },
  webServer: [
    { command: `npx vite --config vite.viewer.config.ts --host ${process.env.VIEWER_TEST_HOST ?? "127.0.0.1"} --port ${port}`,
      url: `http://127.0.0.1:${port}/tests/remote-viewer.html`, reuseExistingServer: false },
    { command: "PYTHONPATH=.. ../.venv/bin/python ../tests/viewer_browser_server.py",
      url: "http://127.0.0.1:4178/healthz", reuseExistingServer: false },
  ],
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "webkit-mobile", use: { ...devices["iPhone 15"] } },
  ],
});
