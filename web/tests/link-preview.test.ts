import assert from "node:assert/strict";
import { createServer } from "vite";
import { isNeutralSilhouette } from "../src/site-icon.ts";

const pixels = (...rgba: number[][]) => new Uint8ClampedArray(rgba.flat());
const clear = [0, 0, 0, 0], dark = [36, 41, 47, 255], white = [255, 255, 255, 255];
assert.equal(isNeutralSilhouette(pixels(clear, dark, dark)), true, "dark transparent logos follow the theme");
assert.equal(isNeutralSilhouette(pixels(clear, white, white)), true, "white logos also adapt to light themes");
assert.equal(isNeutralSilhouette(pixels(clear, dark, [36, 41, 47, 96])), true, "antialiased edges keep their alpha");
assert.equal(isNeutralSilhouette(pixels(clear, [255, 210, 30, 255], dark)), false, "colored artwork retains brand colors");
assert.equal(isNeutralSilhouette(pixels(clear, white, dark)), false, "internal light/dark details are not flattened");
assert.equal(isNeutralSilhouette(pixels(white, dark, dark)), false, "opaque icons keep their own background");
assert.equal(isNeutralSilhouette(pixels(clear, clear)), false, "empty icons are not silhouettes");

const icon = "data:image/png;base64,aWNvbg==";
const originalFetch = globalThis.fetch;
const originalNow = Date.now;
const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { getLinkMetadata, subscribeLinkIcon } = await harness.ssrLoadModule("/src/link-preview.ts") as typeof import("../src/link-preview");
  const requests: { url: string; detail: string }[] = [];
  let releaseIcon!: (response: Response) => void;
  globalThis.fetch = async (_input, init) => {
    const body = JSON.parse(String(init?.body));
    requests.push(body);
    if (body.detail === "icon") return new Promise<Response>((resolve) => { releaseIcon = resolve; });
    return new Response(JSON.stringify({ icon, title: "Documentation" }));
  };
  const seen: string[] = [], other: string[] = [];
  const stop = subscribeLinkIcon("https://example.org/a", (value) => seen.push(value));
  const stopOther = subscribeLinkIcon("https://other.example/b", (value) => other.push(value));
  const automatic = getLinkMetadata("https://example.org/private?a=secret#hash", "icon");
  const duplicate = getLinkMetadata("https://example.org/other", "icon");
  assert.equal(automatic, duplicate);
  assert.deepEqual(requests, [{ url: "https://example.org", detail: "icon" }]);
  await getLinkMetadata("https://example.org/docs", "page");
  assert.deepEqual(seen, [icon], "a mounted sibling updates when a preview discovers the icon");
  assert.deepEqual(other, [], "sharing must stay within the same origin");
  stop(); stopOther();
  releaseIcon(new Response("{}"));
  assert.equal((await automatic).icon, icon, "a late failed icon request cannot overwrite page discovery");
  assert.deepEqual(seen, [icon], "unmounted listeners are released");
  assert.equal((await getLinkMetadata("https://example.org/new", "icon")).icon, icon);
  assert.equal(requests.length, 2, "subsequent links reuse the discovered origin icon");
  const late: string[] = [];
  subscribeLinkIcon("https://example.org/new", (value) => late.push(value))();
  assert.deepEqual(late, [icon], "newly mounted links paint the cached icon immediately");
  let now = originalNow();
  Date.now = () => now;
  let attempts = 0;
  globalThis.fetch = async () => {
    attempts++;
    return new Response(JSON.stringify({ title: "Recoverable", icon,
      ...(attempts > 1 ? { image: icon } : {}) }));
  };
  const partial = await getLinkMetadata("https://partial.example/article", "page");
  assert.equal(partial.image, undefined);
  now += 30_000;
  await getLinkMetadata("https://partial.example/article", "page");
  assert.equal(attempts, 1, "a partial preview is cached briefly, avoiding repeated downloads");
  now += 31_000;
  assert.equal((await getLinkMetadata("https://partial.example/article", "page")).image, icon,
    "a missing cover retries after the short expiry even when the title succeeded");
  now += 61_000;
  await getLinkMetadata("https://partial.example/article", "page");
  assert.equal(attempts, 2, "a complete preview retains the longer cache lifetime");
} finally {
  globalThis.fetch = originalFetch;
  Date.now = originalNow;
  await harness.close();
}
console.log("link preview icon tests passed");
