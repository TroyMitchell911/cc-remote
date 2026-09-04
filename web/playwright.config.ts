import { defineConfig, devices } from "@playwright/test";

const NEW_CHAT_CONTROL_TESTS =
  /new-chat controls|default permission picker|256-character profile id|Work multi-account controls/;
const WEBKIT_LIVE_INTERACTION_TESTS =
  /live append follows|scrolling a live-dirty history window|returning to a background-grown live turn|iOS pointercancel releases process interactions|switching sessions clears retained desktop text selection|nested process disclosures|stationary press opens|dragging a process header|dragging nested process thinking|multi-line IME growth|long paste|oversized edited paste|multi-line composer growth|composer action growth|Codex controls stay on one row|queued messages expand|migration picker/;
const WEBKIT_RENDERING_TESTS =
  /mounted message image|two visible images|HTML preview|artifact-(?:svg|markdown-svg|pdf|gif|invalid-gif)|mobile Markdown source editor|dark desktop code block|Codex settings|Claude settings|history page cache|instant session cache|session cache rejects|canonical image reference|fallback image preview|streaming rerenders|expanded tool batches|Mermaid|chat formulas|real wide Robot|pending composer image|profile keycaps|profile session card edges/;
const WEBKIT_GOAL_PLAN_TESTS = /[Pp]lan|[Gg]oal/;

export default defineConfig({
  testDir: "./tests",
  testMatch: "history-browser.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npx vite --host 127.0.0.1 --port 4174",
    url: "http://127.0.0.1:4174/tests/history-browser.html",
    reuseExistingServer: false,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 900, height: 720 },
      },
    },
    {
      name: "webkit",
      grepInvert: [
        NEW_CHAT_CONTROL_TESTS,
        WEBKIT_LIVE_INTERACTION_TESTS,
        WEBKIT_RENDERING_TESTS,
        WEBKIT_GOAL_PLAN_TESTS,
      ],
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      // Keep every serial WebKit browser lifecycle below its macOS context-churn
      // cliff. A 64th+ context can stall before DOMContentLoaded even though the
      // same test passes alone, so independent rendering and control coverage
      // run in fresh browser processes without reducing the test matrix.
      name: "webkit-rendering",
      grep: WEBKIT_RENDERING_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      name: "webkit-live-interactions",
      grep: WEBKIT_LIVE_INTERACTION_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      name: "webkit-progress",
      grep: WEBKIT_GOAL_PLAN_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
    {
      name: "webkit-controls",
      grep: NEW_CHAT_CONTROL_TESTS,
      use: {
        ...devices["iPhone 15"],
      },
    },
  ],
});
