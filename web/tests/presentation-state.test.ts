import assert from "node:assert/strict";
import { test } from "node:test";
import { manualUnreadKey, readManualUnread, setManualUnread } from "../src/manual-unread.ts";
import { HistoryImageAssetCache, historyImageAssetKey } from "../src/history-image-assets.ts";
import { PROTOCOL_VERSION } from "../src/protocol.ts";

test("manual unread is independent, persistent, bounded and fully scoped", () => {
  const scope = { machineId: "machine-a", engine: "codex" as const, space: "code" as const };
  const marks = setManualUnread({}, scope, "profile-a@session-a", true, 100);
  assert.equal(marks[manualUnreadKey(scope, "profile-a@session-a")], 100);
  assert.equal(marks[manualUnreadKey(scope, "profile-b@session-a")], undefined);
  assert.equal(marks[manualUnreadKey({ ...scope, machineId: "machine-b" }, "profile-a@session-a")], undefined);
  assert.equal(marks[manualUnreadKey({ ...scope, engine: "claude" }, "profile-a@session-a")], undefined);
  assert.equal(marks[manualUnreadKey({ ...scope, space: "work" }, "profile-a@session-a")], undefined);
  assert.deepEqual(readManualUnread({ getItem: () => JSON.stringify(marks) }), marks);
  assert.deepEqual(setManualUnread(marks, scope, "profile-a@session-a", false), {});
  assert.deepEqual(readManualUnread({ getItem: () => '{"bad":true}' }), {});
  let bounded = marks;
  for (let i = 0; i < 600; i++) bounded = setManualUnread(bounded, scope, `session-${i}`, true, i + 1);
  assert.equal(Object.keys(bounded).length, 512);
});

test("a revision-error response settles the exact image request and preserves its explanation", () => {
  const cache = new HistoryImageAssetCache(1);
  cache.begin({ sid: "s", turnId: "t", imageId: "i", variant: "thumbnail", requestId: "r", revision: "old" });
  const error = { v: PROTOCOL_VERSION, ts: 10, type: "history_image" as const,
    session_id: "s", turn_id: "t", image_id: "i", variant: "thumbnail" as const,
    request_id: "r", revision: "new", error: "会话历史已更新，请重新加载图片" };
  assert.equal(cache.accept({ ...error, session_id: "wrong" }), false);
  assert.equal(cache.accept({ ...error, error: null, media_type: "image/png", data: "not-accepted" }), false);
  assert.equal(cache.accept(error), true);
  assert.equal(cache.forSession("s")[historyImageAssetKey("t", "i", "thumbnail")].error, error.error);
  assert.equal(cache.has("s", "t", "i", "thumbnail"), false);
  assert.equal(cache.accept(error), false);
  assert.equal(cache.begin({ sid: "s", turnId: "t", imageId: "i", variant: "thumbnail", requestId: "retry", revision: "new" }), true);
  cache.clear();
});
