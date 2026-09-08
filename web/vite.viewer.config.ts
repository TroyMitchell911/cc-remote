import { defineConfig } from "vite";
import base from "./vite.config";

// Keep even page-unload/keepalive traffic inside the model-free test fixture.
// Such requests can outlive Playwright's page routes; never send them to a
// developer's ordinary relay on :8765.
export default defineConfig({
  ...base,
  server: { ...base.server, proxy: {
    "/api": { target: "http://127.0.0.1:4178" },
    "/ws": { target: "ws://127.0.0.1:4178", ws: true },
    "/__cc_viewer": { target: "http://127.0.0.1:4178" },
    "/cc-remote-viewer-runner.js": { target: "http://127.0.0.1:4178" },
  } },
});
