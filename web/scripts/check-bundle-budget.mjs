import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { gzipSync } from "node:zlib";

const DIST = resolve(import.meta.dirname, "../dist");
// Selection tracking, async routing, parent-scoped BTW visibility and lazy
// loaders add <4 KiB to startup. The ~164 KiB HTML parser and form stay lazy.
const MAX_ENTRY_BYTES = 518 * 1024;
const MAX_INITIAL_BYTES = 912 * 1024;
const MAX_INITIAL_GZIP_BYTES = 280 * 1024;
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
