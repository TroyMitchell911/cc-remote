import { defineConfig } from "vite";
import { resolve } from "node:path";

// A classic, self-contained script works inside an opaque-origin frame without
// granting credentialed CORS to app assets. Parsers never enter the chat bundle.
export default defineConfig({
  publicDir: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: resolve(import.meta.dirname, "src/viewer-runner/index.ts"),
      name: "CCRemoteViewerRunner", formats: ["iife"],
      fileName: () => "cc-remote-viewer-runner.js",
    },
  },
});
