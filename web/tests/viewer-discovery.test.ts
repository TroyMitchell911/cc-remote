import assert from "node:assert/strict";
import { test } from "node:test";
import { ViewerDiscoveryQueue } from "../src/viewer-discovery.ts";
import type { ViewerPage } from "../src/remote-viewer.ts";
import { ViewerPagesCache } from "../src/viewer-pages-cache.ts";

function page(path: string, available = true): ViewerPage {
  return { id: "page", machine_id: "device", site_id: "site", entry: path, label: "Page",
    references: [path], turn_ids: [], available };
}

test("page display cache restores only the exact scope and bounds metadata", () => {
  const cache = new ViewerPagesCache();
  const scope = { machineId: "machine-a", engine: "codex", space: "code", sid: "profile-a@sid" };
  const pages = [page("/index.html")];
  cache.set(scope, pages);
  assert.equal(cache.get(scope), pages);
  for (const other of [{ machineId: "machine-b" }, { engine: "claude" }, { space: "work" }, { sid: "profile-b@sid" }])
    assert.equal(cache.get({ ...scope, ...other }), undefined);
  cache.set(scope, []);
  assert.deepEqual(cache.get(scope), [], "authoritative empty lists remove stale entries");
  for (let i = 0; i < 33; i++) cache.set({ ...scope, sid: `session-${i}` }, pages);
  assert.equal(cache.get(scope), undefined);
  assert.equal(cache.get({ ...scope, sid: "session-32" }), pages);
  cache.clear();
  assert.equal(cache.get({ ...scope, sid: "session-32" }), undefined);
});

test("a negative lookup retries without another render or session switch", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/index.html"], "turn", false, 0);
  const first = queue.take(0)!;
  assert.deepEqual(first.paths, ["/index.html"]);
  queue.settle(first, [], 10);
  assert.equal(queue.nextAt(), 510);
  assert.equal(queue.take(509), undefined);
  const retry = queue.take(510)!;
  queue.settle(retry, [page("/index.html")], 520);
  assert.equal(queue.nextAt(), Infinity);
  queue.add(["/index.html"], "turn", true, 600);
  assert.equal(queue.take(600), undefined, "confirmed pages are not re-resolved at completion");
});

test("completion during an in-flight lookup is not lost", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/index.html"], "turn", false, 0);
  const first = queue.take(0)!;
  queue.add(["/index.html"], "turn", true, 5);
  assert.equal(queue.take(5), undefined, "never run the same hint concurrently");
  queue.settle(first, [], 10);
  assert.equal(queue.nextAt(), 5, "the final boundary is ready immediately, not consumed by the old response");
  const final = queue.take(10)!;
  queue.settle(final, [page("/index.html")], 20);
  assert.equal(queue.nextAt(), Infinity);
});

test("unresolved hints stop retrying, but get a fresh bounded completion attempt", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/missing.html"], "turn", false, 0);
  for (const now of [0, 500, 2000, 6000]) {
    const batch = queue.take(now)!;
    assert.ok(batch);
    queue.settle(batch, [], now);
  }
  assert.equal(queue.nextAt(), Infinity);
  queue.add(["/missing.html"], "turn", false, 7000);
  assert.equal(queue.take(7000), undefined);
  queue.add(["/missing.html"], "turn", true, 8000);
  for (const now of [8000, 8500, 10000, 14000]) {
    queue.settle(queue.take(now)!, [], now);
  }
  queue.add(["/missing.html"], "turn", true, 15000);
  assert.equal(queue.nextAt(), Infinity, "repeated completed renders cannot create a retry loop");
});

test("unavailable associations are not treated as confirmed", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/index.html"], "turn", true, 0);
  queue.settle(queue.take(0)!, [page("/index.html", false)], 0);
  assert.equal(queue.nextAt(), 500);
});

test("batches stay within eight paths and one native turn", () => {
  const queue = new ViewerDiscoveryQueue();
  const paths = Array.from({ length: 10 }, (_, i) => `/${i}.html`);
  queue.add(paths, "first", false, 0);
  queue.add(["/other.html"], "second", false, 0);
  const first = queue.take(0)!;
  assert.equal(first.turnId, "first");
  assert.equal(first.paths.length, 8);
  queue.settle(first, first.paths.map((path) => page(path)), 0);
  const second = queue.take(0)!;
  assert.equal(second.turnId, "first");
  assert.equal(second.paths.length, 2);
  queue.settle(second, second.paths.map((path) => page(path)), 0);
  assert.equal(queue.take(0)!.turnId, "second");
});

test("references dedupe per turn without starving newly arriving paths behind a retry", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/missing.html", "/missing.html"], "first", false, 0);
  const first = queue.take(0)!;
  assert.equal(first.paths.length, 1);
  queue.settle(first, [], 0);
  queue.add(["/ready.html"], "second", false, 100);
  assert.equal(queue.nextAt(), 100);
  assert.deepEqual(queue.take(100)!.paths, ["/ready.html"]);
});

test("an older retry does not overtake a ready path after a slow request", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(["/missing.html"], "first", false, 0);
  queue.settle(queue.take(0)!, [], 0);
  queue.add(["/ready.html"], "second", false, 100);
  assert.deepEqual(queue.take(1000)!.paths, ["/ready.html"]);
});

test("hint storage remains bounded even when a long history is mounted", () => {
  const queue = new ViewerDiscoveryQueue();
  queue.add(Array.from({ length: 2050 }, (_, i) => `/${i}.html`), "turn", true, 0);
  let count = 0;
  for (let batch = queue.take(0); batch; batch = queue.take(0)) {
    count += batch.paths.length;
    queue.settle(batch, [], 0);
  }
  assert.equal(count, 2048);
});
