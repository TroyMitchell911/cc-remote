import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { gzipSync } from "node:zlib";

const DIST = resolve(import.meta.dirname, "../dist");
// Selection tracking, async routing, parent-scoped BTW visibility and lazy
// loaders add <4 KiB to startup. The ~164 KiB HTML parser and form stay lazy.
// Native generated-output thumbnails reuse existing image loaders/lightbox;
// their presentation and accessible labels add <2 KiB, no new dependencies.
// Exact supplemental-answer presentation, full-width image actions and bounded
// automatic preview reads add <3 KiB; keep the gzip/dependency-count gates fixed.
// Remote Viewer adds scoped descriptors, a menu/link entry and panel dispatch
// (~4 KiB initial JS). The iframe UI/styles are lazy; no browser/CAD dependency
// is added to startup. Keep compressed-size and request-count limits unchanged.
// Verified session-page links add <0.2 KiB of click dispatch; discovery and
// import-map compilation remain lazy. Keep gzip and request-count gates fixed.
// First-paint page metadata restoration and correlated image errors add <2 KiB.
// Manual unread stays in the lazy sidebar; no new startup dependency/request.
// Folded per-turn changes and persistent model-fallback notes add <2 KiB.
// No new runtime dependency; gzip and initial request budgets stay fixed.
// Typed file badges and shared-prefix path labels add <2 KiB of local UI code.
// Entry, gzip and request-count caps remain unchanged; no icon dependency.
// Bounded file pagination, scoped request correlation and retry UI add <4 KiB.
// No new dependencies; gzip and initial request-count caps remain unchanged.
// Steer item ownership, explicit Goal recovery and shared clipboard/UI state
// add <2 KiB at startup. Attachment decoding and clipboard byte imports load
// on demand; retain the compressed-size and initial request-count limits.
// Provider-capacity copy and terminal outcome labels add <1 KiB of startup JS.
// The card has no new dependencies; gzip and initial request caps stay unchanged.
// Quota failure copy and validated native retry dates add <1 KiB at startup.
// Keep entry, compressed-size, and request-count caps unchanged.
// Native compaction animation and exact boundary replacement add <1 KiB of
// startup JS. Keep entry, compressed-size and request-count limits unchanged.
const MAX_ENTRY_BYTES = 537 * 1024;
// Viewport portals for image/paste overlays keep mobile page layers stable.
// They add <0.1 KiB, with no extra request/dependency; round the cap to a KiB.
// Shared Claude/Codex continuation splitting and the existing task dock add
// <1 KiB. No new dependency/request; keep entry and compressed-size caps fixed.
// Centered BTW shortcuts, viewport anchoring and shared attachment icons add
// ~1.1 KiB initial JS / 0.4 KiB gzip. Panel UI/styles remain lazy, with no new
// dependency or initial request; retain the entry and request-count caps.
// Shared mutually exclusive path popovers and the lazy webpage-link entry add
// <3 KiB / <1 KiB gzip. Metadata fetching and rich cards stay in a lazy chunk.
// No runtime dependency or extra initial request; retain entry/file-count caps.
// Harmony push availability and notification routing add <2 KiB at startup.
// Provider and Web Push operations load on demand, without a runtime dependency
// or an extra initial request; retain the entry/file-count caps.
// Session Artifacts entry/routing and an independent centered-chat layer add
// <3 KiB; catalog, preview UI and styles load on demand. Gzip/request caps stay fixed.
// Brand symbols are external SVG, not an icon-library dependency. Status-card
// markup adds <1 KiB JS / 0.3 KiB gzip; retain entry and initial request caps.
// Global harness navigation and scoped modal/side-chat restoration add <1 KiB.
// Question draft UI remains lazy; gzip, entry and request caps stay unchanged.
// Cross-engine message references and on-demand catalog routing add <1 KiB.
// Receipt parsing and link UI stay lazy; gzip/request limits are unchanged.
// Per-engine/mode appearance migration and derived colors add <6 KiB at startup
// so saved colors apply before paint. The editor remains lazy; no new dependency
// or initial request. Allow <2 KiB gzip for the persistence/color calculations.
const MAX_INITIAL_BYTES = 958 * 1024;
// Cross-thread provenance, scoped navigation and receipts add <2 KiB gzip.
// The history reader and link UI load on demand; other caps stay unchanged.
// DSH engine routing/defaults and native return-receipt boundaries add <1 KiB
// gzip. Management panels and xterm stay lazy; retain entry/raw/request caps.
// Paragraph reading anchors and immediate virtual-row measurement add <1 KiB
// gzip. Keep native viewport resize handling, with no new dependency/request.
const MAX_INITIAL_GZIP_BYTES = 290 * 1024;
const MAX_INITIAL_JS_FILES = 4;

const html = readFileSync(resolve(DIST, "index.html"), "utf8");
const entryMatch = html.match(
  /<script\b[^>]*\btype="module"[^>]*\bsrc="([^"]+\.js)"[^>]*>/,
);
if (!entryMatch) throw new Error("production entry script is missing");

const initialUrls = new Set([entryMatch[1]]);
for (const match of html.matchAll(
  /<link\b[^>]*\brel="modulepreload"[^>]*\bhref="([^"]+\.js)"[^>]*>/g,
)) {
  initialUrls.add(match[1]);
}

const rows = [...initialUrls].map((url) => {
  if (!url.startsWith("/assets/")) {
    throw new Error(`unexpected initial script URL: ${url}`);
  }
  const file = resolve(DIST, url.slice(1));
  return {
    url,
    bytes: statSync(file).size,
    gzipBytes: gzipSync(readFileSync(file), { level: 9 }).byteLength,
  };
});
const entry = rows.find((row) => row.url === entryMatch[1]);
const totalBytes = rows.reduce((sum, row) => sum + row.bytes, 0);
const totalGzipBytes = rows.reduce((sum, row) => sum + row.gzipBytes, 0);

const kib = (bytes) => `${(bytes / 1024).toFixed(1)} KiB`;
console.log(
  `bundle budget: entry ${kib(entry.bytes)}, initial ${kib(totalBytes)}`
  + ` / ${kib(totalGzipBytes)} gzip across ${rows.length} files`,
);

const violations = [];
if (entry.bytes > MAX_ENTRY_BYTES) {
  violations.push(`entry ${kib(entry.bytes)} exceeds ${kib(MAX_ENTRY_BYTES)}`);
}
if (totalBytes > MAX_INITIAL_BYTES) {
  violations.push(
    `initial JS ${kib(totalBytes)} exceeds ${kib(MAX_INITIAL_BYTES)}`,
  );
}
if (totalGzipBytes > MAX_INITIAL_GZIP_BYTES) {
  violations.push(
    `initial gzip ${kib(totalGzipBytes)} exceeds ${kib(MAX_INITIAL_GZIP_BYTES)}`,
  );
}
if (rows.length > MAX_INITIAL_JS_FILES) {
  violations.push(
    `initial JS uses ${rows.length} files; maximum is ${MAX_INITIAL_JS_FILES}`,
  );
}
if (violations.length > 0) {
  throw new Error(`bundle budget exceeded:\n- ${violations.join("\n- ")}`);
}
