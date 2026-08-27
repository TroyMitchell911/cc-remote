import assert from "node:assert/strict";
import {
  HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  HistoryDetailRequestCoordinator,
  HistoryRequestCoordinator,
  resolveHistoryCwdHint,
  type HistoryBrowseRequestContext,
  type HistoryDetailRequestContext,
} from "../src/history-requests.ts";
import { RecoverableReadCoordinator } from "../src/recoverable-read.ts";
import {
  HistoryImageAssetCache,
  historyImageAssetKey,
} from "../src/history-image-assets.ts";

let now = 1_000;
const coordinator = new HistoryRequestCoordinator(() => now, 500);
let sends = 0;
const send = () => { sends += 1; return true; };

coordinator.beginConnection();
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
}, send), true);
// ReplayStart must replace a generic focus read already on the wire. Relabelling
// that request would let its older-generation response strand recovery forever.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), true);
assert.equal(sends, 2);
coordinator.complete({
  session_id: "session-1", generation: "generation-0",
});
assert.equal(coordinator.size(), 1,
  "the response to the generic request cannot complete its generation-bound replacement");
coordinator.complete({
  session_id: "session-1",
});
assert.equal(coordinator.size(), 1,
  "an untagged response cannot complete a generation-bound replacement");
assert.equal(coordinator.request({
  sid: "session-1", limit: 4, generation: "generation-1",
}, send), false);
assert.equal(sends, 2);

// Pagination and another session are independent.
assert.equal(coordinator.request({
  sid: "session-1", before: "turn-5", limit: 12,
}, send), true);
assert.equal(coordinator.request({
  sid: "session-2", limit: 4,
}, send), true);
assert.equal(sends, 4);

const acceptedHistoryLists = {
  "code:claude": [{
    session_id: "session-1",
    engine: "claude" as const,
    space: "code" as const,
    cwd: "/project/one",
  }],
  "work:claude": [{
    session_id: "session-2",
    engine: "claude" as const,
    space: "work" as const,
    cwd: "/project/two",
  }],
};
assert.equal(
  resolveHistoryCwdHint(acceptedHistoryLists, "session-1"),
  "/project/one",
);
assert.equal(resolveHistoryCwdHint(acceptedHistoryLists, "missing"), undefined);
assert.equal(resolveHistoryCwdHint({
  ...acceptedHistoryLists,
  "code:codex": [{
    session_id: "session-1",
    engine: "codex" as const,
    space: "code" as const,
    cwd: "/different/project",
  }],
}, "session-1"), undefined,
"conflicting accepted scopes must omit the optional hint");

// An older response must not clear a rollback-bound replacement request.
assert.equal(coordinator.request({
  sid: "session-1", limit: 4,
  generation: "generation-1", revision: "revision-2",
}, send), true);
assert.equal(sends, 5);
coordinator.complete({
  session_id: "session-1", generation: "generation-1",
  revision: "revision-1",
});
assert.equal(coordinator.size(), 3);
coordinator.complete({
  session_id: "session-1", generation: "generation-1",
  revision: "revision-2",
});
assert.equal(coordinator.size(), 2);

// A new socket and a timed-out request may retry exactly once.
coordinator.beginConnection();
assert.equal(coordinator.size(), 0);
assert.equal(coordinator.request({ sid: "session-1", limit: 4 }, send), true);
now += 600;
assert.equal(coordinator.request({ sid: "session-1", limit: 4 }, send), true);
assert.equal(sends, 7);

const rejectedSendCoordinator = new HistoryRequestCoordinator(() => now, 500);
rejectedSendCoordinator.beginConnection();
assert.equal(rejectedSendCoordinator.request(
  { sid: "rejected-send", before: "cursor", limit: 12 },
  () => false,
), false);
assert.equal(rejectedSendCoordinator.size(), 0,
  "an outbox rejection must not leave a phantom in-flight history request");
assert.equal(rejectedSendCoordinator.request(
  { sid: "rejected-send", before: "cursor", limit: 12 },
  () => true,
), true, "the next pagination gesture may retry after a rejected enqueue");

// A wire pagination read may be shared by two local browse epochs, but the
// immutable request-time contexts must remain distinct. The response handler
// decides which waiter still owns the visible projection; it must never stamp
// the response with whichever session/surface happens to be current on arrival.
const browseCoordinator = new HistoryRequestCoordinator(() => now, 500);
const browseSends: string[] = [];
const browseContext = (
  viewId: string,
  windowEpoch: number,
  scopeKey = "machine-a:code:codex",
): HistoryBrowseRequestContext => ({
  scopeKey,
  viewId,
  windowEpoch,
  pendingBefore: "turn-5",
  sourcePageKey: "head-page",
});
browseCoordinator.beginConnection();
assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-a", revision: "revision-a",
  browse: browseContext("view-old", 1),
}, () => { browseSends.push("wire"); return true; }), true);
assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-a", revision: "revision-a",
  browse: browseContext("view-new", 2),
}, () => { browseSends.push("wire"); return true; }), true,
"a newer local browse epoch may wait on the already in-flight wire page");
assert.deepEqual(browseSends, ["wire"],
"local browse waiters must not duplicate the server scan");
assert.deepEqual(
  browseCoordinator.complete({
    session_id: "session-browse", before: "turn-5",
    generation: "generation-a", revision: "revision-a",
  }).matched.map((context) => [
    context.scopeKey, context.viewId, context.windowEpoch,
  ]),
  [
    ["machine-a:code:codex", "view-old", 1],
    ["machine-a:code:codex", "view-new", 2],
  ],
);
assert.equal(browseCoordinator.size(), 0);

assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-b", revision: "revision-b",
  browse: browseContext("view-revision-b", 3),
}, () => { browseSends.push("revision-b"); return true; }), true);
assert.deepEqual(browseCoordinator.complete({
  session_id: "session-browse", before: "turn-5",
  generation: "generation-b", revision: "revision-a",
}), {
  matched: [],
  stale: [browseContext("view-revision-b", 3)],
}, "an old revision releases, but cannot install into, a newer browse waiter");
assert.equal(browseCoordinator.size(), 0);
assert.equal(browseCoordinator.request({
  sid: "session-browse", before: "turn-5", limit: 12,
  generation: "generation-b", revision: "revision-b",
  browse: browseContext("view-before-reconnect", 4),
}, () => true), true);
assert.deepEqual(browseCoordinator.beginConnection(), [{
  sid: "session-browse",
  generation: "generation-b",
  revision: "revision-b",
  browse: browseContext("view-before-reconnect", 4),
}], "a reconnect returns the exact browse waiter so its spinner can settle");
assert.equal(browseCoordinator.request({
  sid: "session-browse", limit: 4,
  generation: "generation-c", revision: "revision-c",
}, () => true), true);
assert.deepEqual(browseCoordinator.complete({
  session_id: "session-browse", before: "turn-5",
  generation: "generation-b", revision: "revision-b",
}), { matched: [], stale: [] },
"a response from the previous socket has no local browse authority");
assert.equal(browseCoordinator.size(), 1,
  "the obsolete page response cannot clear the new connection's head read");

// A reconnect may immediately retry the exact same pagination cursor. Keep the
// old request's authority long enough to classify its delayed response without
// consuming or releasing the new connection's waiter.
const sameCursorCoordinator = new HistoryRequestCoordinator(() => now, 500);
assert.equal(sameCursorCoordinator.request({
  sid: "same-cursor", before: "turn-9", limit: 12,
  generation: "generation-old", revision: "revision-old",
  browse: browseContext("view-old-connection", 5),
}, () => true), true);
assert.deepEqual(sameCursorCoordinator.beginConnection(), [{
  sid: "same-cursor",
  generation: "generation-old",
  revision: "revision-old",
  browse: browseContext("view-old-connection", 5),
}]);
assert.equal(sameCursorCoordinator.request({
  sid: "same-cursor", before: "turn-9", limit: 12,
  generation: "generation-new", revision: "revision-new",
  browse: browseContext("view-new-connection", 6),
}, () => true), true);
assert.deepEqual(sameCursorCoordinator.complete({
  session_id: "same-cursor", before: "turn-9",
  generation: "generation-old", revision: "revision-old",
}), { matched: [], stale: [] });
assert.equal(sameCursorCoordinator.size(), 1,
  "a delayed same-cursor response must preserve the replacement waiter");
assert.deepEqual(sameCursorCoordinator.complete({
  session_id: "same-cursor", before: "turn-9",
  generation: "generation-new", revision: "revision-new",
}), {
  matched: [browseContext("view-new-connection", 6)],
  stale: [],
});
assert.equal(sameCursorCoordinator.size(), 0);

const replacementCancellations: unknown[] = [];
assert.equal(sameCursorCoordinator.request({
  sid: "same-cursor", before: "turn-9", limit: 12,
  generation: "generation-a", revision: "revision-a",
  browse: browseContext("view-replaced", 7),
}, () => true), true);
assert.equal(sameCursorCoordinator.request({
  sid: "same-cursor", before: "turn-9", limit: 12,
  generation: "generation-b", revision: "revision-b",
  browse: browseContext("view-replacement", 8),
}, () => true, (cancelled) => replacementCancellations.push(...cancelled)), true);
assert.deepEqual(replacementCancellations, [{
  sid: "same-cursor",
  generation: "generation-a",
  revision: "revision-a",
  browse: browseContext("view-replaced", 7),
}], "a successful replacement settles the displaced spinner immediately");
assert.deepEqual(sameCursorCoordinator.complete({
  session_id: "same-cursor", before: "turn-9",
  generation: "generation-a", revision: "revision-a",
}), { matched: [], stale: [] });
assert.equal(sameCursorCoordinator.size(), 1);

// Two retired reads require two responses to exhaust their authority. The
// first delayed response must not erase every tombstone and expose the active
// replacement to the second one.
const duplicateOldCoordinator = new HistoryRequestCoordinator(() => now, 500);
for (let epoch = 0; epoch < 2; epoch += 1) {
  assert.equal(duplicateOldCoordinator.request({
    sid: "duplicate-old", before: "turn-11", limit: 12,
    generation: "generation-old", revision: "revision-old",
  }, () => true), true);
  duplicateOldCoordinator.beginConnection();
}
assert.equal(duplicateOldCoordinator.request({
  sid: "duplicate-old", before: "turn-11", limit: 12,
  generation: "generation-new", revision: "revision-new",
  browse: browseContext("view-after-two-reconnects", 9),
}, () => true), true);
for (let response = 0; response < 2; response += 1) {
  assert.deepEqual(duplicateOldCoordinator.complete({
    session_id: "duplicate-old", before: "turn-11",
    generation: "generation-old", revision: "revision-old",
  }), { matched: [], stale: [] });
  assert.equal(duplicateOldCoordinator.size(), 1);
}

// A reconnect can retire every command in RelayWs's 256-command reliable
// outbox. Authorities must not expire merely because the transcript scan took
// longer than the normal coalescing timeout, and the old 64-entry ceiling must
// not expose a same-cursor replacement.
const saturatedRetiredCoordinator = new HistoryRequestCoordinator(
  () => now,
  500,
);
for (let index = 0; index < 256; index += 1) {
  assert.equal(saturatedRetiredCoordinator.request({
    sid: `retired-${index}`, before: `turn-${index}`, limit: 12,
    generation: "generation-old", revision: "revision-old",
  }, () => true), true);
}
saturatedRetiredCoordinator.beginConnection();
now += 10_000;
assert.equal(saturatedRetiredCoordinator.request({
  sid: "retired-0", before: "turn-0", limit: 12,
  generation: "generation-new", revision: "revision-new",
  browse: browseContext("saturated-replacement", 10),
}, () => true), true);
assert.deepEqual(saturatedRetiredCoordinator.complete({
  session_id: "retired-0", before: "turn-0",
  generation: "generation-old", revision: "revision-old",
}), { matched: [], stale: [] });
assert.equal(saturatedRetiredCoordinator.size(), 1,
  "the oldest in-flight authority survives a saturated reconnect and slow scan");
assert.deepEqual(saturatedRetiredCoordinator.complete({
  session_id: "retired-0", before: "turn-0",
  generation: "generation-new", revision: "revision-new",
}), {
  matched: [browseContext("saturated-replacement", 10)],
  stale: [],
});

const retainedReplacementCancellations: unknown[] = [];
assert.equal(sameCursorCoordinator.request({
  sid: "same-cursor", before: "turn-9", limit: 12,
  generation: "generation-c", revision: "revision-c",
  browse: browseContext("view-replacement", 8),
}, () => true, (cancelled) =>
  retainedReplacementCancellations.push(...cancelled)), true);
assert.deepEqual(retainedReplacementCancellations, [],
  "the same local waiter transfers to its replacement without flickering idle");

const detailCoordinator = new HistoryDetailRequestCoordinator();
assert.equal(detailCoordinator.begin({
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
}), true);
assert.equal(detailCoordinator.begin({
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
}), false, "one wire detail read must serve one frozen browse target");
assert.equal(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-old",
  turn_id: "older-turn",
}), null, "a stale detail response cannot consume the current browse target");
assert.deepEqual(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-a",
  turn_id: "older-turn",
}), {
  target: "browse",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "older-turn",
  viewId: "view-new",
  windowEpoch: 2,
});

const sharedDetailCoordinator = new HistoryDetailRequestCoordinator();
const sharedRuntimeTarget = {
  target: "runtime" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-shared-detail",
  revision: "revision-a",
  turnId: "shared-turn",
  autoLoad: true,
};
const sharedBrowseTarget = {
  target: "browse" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-shared-detail",
  revision: "revision-a",
  turnId: "shared-turn",
  viewId: "browse-view",
  windowEpoch: 3,
};
assert.deepEqual(sharedDetailCoordinator.register(sharedRuntimeTarget), {
  accepted: true, send: true,
});
assert.deepEqual(sharedDetailCoordinator.register(sharedBrowseTarget), {
  accepted: true, send: false,
}, "a user browse target joins an already-running runtime detail read");
assert.deepEqual(sharedDetailCoordinator.completeAll({
  session_id: sharedRuntimeTarget.sid,
  revision: sharedRuntimeTarget.revision,
  turn_id: sharedRuntimeTarget.turnId,
}), [sharedRuntimeTarget, sharedBrowseTarget],
"one wire response is delivered to every frozen projection waiter");
const newestDetailContext = {
  target: "runtime" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "paged-turn",
};
const olderDetailContext = {
  ...newestDetailContext,
  before: "detail-cursor-older",
};
assert.equal(detailCoordinator.begin(newestDetailContext), true);
assert.equal(detailCoordinator.begin(olderDetailContext), true,
  "different intra-turn cursors need independent frozen request authority");
assert.equal(detailCoordinator.begin(olderDetailContext), false,
  "the same intra-turn cursor still deduplicates one wire read");
assert.deepEqual(detailCoordinator.complete({
  session_id: newestDetailContext.sid,
  revision: newestDetailContext.revision,
  turn_id: newestDetailContext.turnId,
}), newestDetailContext,
  "the newest detail response consumes only the cursorless request");
assert.equal(detailCoordinator.complete({
  session_id: olderDetailContext.sid,
  revision: olderDetailContext.revision,
  turn_id: olderDetailContext.turnId,
  before: "another-detail-cursor",
}), null, "a response for another detail cursor cannot consume the pending page");
assert.deepEqual(detailCoordinator.complete({
  session_id: olderDetailContext.sid,
  revision: olderDetailContext.revision,
  turn_id: olderDetailContext.turnId,
  before: olderDetailContext.before,
}), olderDetailContext,
  "the response before cursor is part of the exact coordinator key");

const resetDetailCoordinator = new HistoryDetailRequestCoordinator();
const resetRuntimeContext = {
  ...newestDetailContext,
  turnId: "reset-turn",
};
const resetOlderContext = {
  ...resetRuntimeContext,
  before: "stale-snapshot-cursor",
};
const retainedOtherTurn = {
  ...resetRuntimeContext,
  turnId: "unrelated-turn",
};
assert.deepEqual(resetDetailCoordinator.register(resetRuntimeContext), {
  accepted: true, send: true,
});
assert.deepEqual(resetDetailCoordinator.register(resetOlderContext), {
  accepted: true, send: true,
});
assert.deepEqual(resetDetailCoordinator.register(retainedOtherTurn), {
  accepted: true, send: true,
});
assert.deepEqual(resetDetailCoordinator.cancelTurn({
  sid: resetRuntimeContext.sid,
  revision: resetRuntimeContext.revision,
  turnId: resetRuntimeContext.turnId,
}), [resetRuntimeContext, resetOlderContext],
"snapshot reset cancels every stale page waiter for only that exact turn");
assert.deepEqual(resetDetailCoordinator.completeAll({
  session_id: resetRuntimeContext.sid,
  revision: resetRuntimeContext.revision,
  turn_id: resetRuntimeContext.turnId,
}), [], "a late stale response cannot re-enter the replacement projection");
assert.deepEqual(resetDetailCoordinator.completeAll({
  session_id: retainedOtherTurn.sid,
  revision: retainedOtherTurn.revision,
  turn_id: retainedOtherTurn.turnId,
}), [retainedOtherTurn], "resetting one turn cannot cancel another detail read");

detailCoordinator.begin({
  target: "runtime",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "latest-turn",
});
assert.deepEqual(detailCoordinator.clear(), [{
  target: "runtime",
  scopeKey: "machine-a:code:codex",
  sid: "session-browse",
  revision: "revision-a",
  turnId: "latest-turn",
}], "clearing frozen targets returns the exact loading rows App must release");
assert.equal(detailCoordinator.complete({
  session_id: "session-browse",
  revision: "revision-a",
  turn_id: "latest-turn",
}), null, "navigation/reconnect must revoke frozen detail authority");

let nextDetailTimer = 1;
const detailTimers = new Map<number, {
  callback: () => void;
  delayMs: number;
}>();
const cancelledDetailTimers: number[] = [];
const timedOutDetailContexts: HistoryDetailRequestContext[] = [];
const expiringDetailCoordinator = new HistoryDetailRequestCoordinator(
  (context) => timedOutDetailContexts.push(context),
  (callback, delayMs) => {
    const timer = nextDetailTimer++;
    detailTimers.set(timer, { callback, delayMs });
    return timer;
  },
  (timer) => {
    const id = timer as number;
    cancelledDetailTimers.push(id);
    detailTimers.delete(id);
  },
);
const expiringDetailContext = {
  target: "runtime" as const,
  scopeKey: "machine-a:code:codex",
  sid: "session-timeout",
  revision: "revision-timeout",
  turnId: "turn-timeout",
};
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true);
assert.equal(detailTimers.get(1)?.delayMs, HISTORY_DETAIL_REQUEST_TIMEOUT_MS);
detailTimers.get(1)?.callback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "a lost response must release the exact frozen detail row");
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true,
  "the user can retry after a silent detail timeout");
const completedTimerCallback = detailTimers.get(2)!.callback;
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turn_id: expiringDetailContext.turnId,
}), expiringDetailContext);
assert.ok(cancelledDetailTimers.includes(2),
  "a completed response must cancel its liveness timer");
assert.equal(expiringDetailCoordinator.begin(expiringDetailContext), true);
completedTimerCallback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "a completed timer callback cannot later release its same-key retry");
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turn_id: expiringDetailContext.turnId,
}), expiringDetailContext,
  "the same-key retry remains pending after the old timer callback");

const clearRuntimeContext = {
  ...expiringDetailContext,
  turnId: "turn-clear-runtime",
};
const clearBrowseContext = {
  target: "browse" as const,
  scopeKey: expiringDetailContext.scopeKey,
  sid: expiringDetailContext.sid,
  revision: expiringDetailContext.revision,
  turnId: "turn-clear-browse",
  viewId: "view-clear",
  windowEpoch: 7,
};
assert.equal(expiringDetailCoordinator.begin(clearRuntimeContext), true);
assert.equal(expiringDetailCoordinator.begin(clearBrowseContext), true);
const clearedRuntimeCallback = detailTimers.get(4)!.callback;
const clearedBrowseCallback = detailTimers.get(5)!.callback;
assert.deepEqual(expiringDetailCoordinator.clear(), [
  clearRuntimeContext, clearBrowseContext,
], "clear returns every runtime and browse loading row");
assert.ok(cancelledDetailTimers.includes(4));
assert.ok(cancelledDetailTimers.includes(5));
assert.equal(expiringDetailCoordinator.begin(clearRuntimeContext), true);
clearedRuntimeCallback();
clearedBrowseCallback();
assert.deepEqual(timedOutDetailContexts, [expiringDetailContext],
  "callbacks cancelled by navigation cannot touch a later request");
assert.deepEqual(expiringDetailCoordinator.complete({
  session_id: clearRuntimeContext.sid,
  revision: clearRuntimeContext.revision,
  turn_id: clearRuntimeContext.turnId,
}), clearRuntimeContext);

let nextTimer = 1;
const scheduled = new Map<number, () => void>();
const scheduledDelays = new Map<number, number>();
const repair = new RecoverableReadCoordinator(
  (callback, delayMs) => {
    const timer = nextTimer++;
    scheduled.set(timer, callback);
    scheduledDelays.set(timer, delayMs);
    return timer;
  },
  (timer) => {
    scheduled.delete(timer);
    scheduledDelays.delete(timer);
  },
  250,
);
let repairs = 0;
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true);
assert.equal(scheduledDelays.get(1), 250,
  "ordinary failures retain the short default retry");
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), false,
  "duplicate failures cannot schedule parallel repair reads");
assert.equal(scheduled.size, 1);
const firstRepair = scheduled.get(1);
scheduled.delete(1);
scheduledDelays.delete(1);
firstRepair?.();
assert.equal(repairs, 1);
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true,
  "a still-growing transcript gets a bounded second repair attempt");
assert.equal(scheduledDelays.get(2), 1_000,
  "repair retries back off instead of polling the transcript");
const secondRepair = scheduled.get(2);
scheduled.delete(2);
scheduledDelays.delete(2);
secondRepair?.();
assert.equal(repairs, 2);
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true);
assert.equal(scheduledDelays.get(3), 4_000);
const thirdRepair = scheduled.get(3);
scheduled.delete(3);
scheduledDelays.delete(3);
thirdRepair?.();
assert.equal(repairs, 3);
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), false,
  "the third failed repair response must stop instead of looping");
assert.equal(repair.retry("detail:turn-1", () => { repairs += 1; }), true,
  "a later explicit request starts a fresh bounded repair cycle");
repair.complete("detail:turn-1");
assert.equal(scheduled.size, 0,
  "an authoritative response cancels a repair that has not fired");

assert.equal(repair.retry("history:page-1", () => { repairs += 1; }), true);
repair.clear();
assert.equal(scheduled.size, 0,
  "disconnect cleanup cancels every pending repair");

const movingPreviewKey = "history:moving-preview";
assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), true);
const cancelledWatchdog = [...scheduled.keys()][0];
assert.equal(scheduledDelays.get(cancelledWatchdog), 60_000,
  "a moving newest-page preview may override the retry with a slow watchdog");
repair.complete(movingPreviewKey);
assert.equal(scheduled.size, 0,
  "an authoritative background refresh cancels the slow watchdog");

assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), true);
const firedWatchdog = [...scheduled.keys()][0];
const watchdogRead = scheduled.get(firedWatchdog);
scheduled.delete(firedWatchdog);
scheduledDelays.delete(firedWatchdog);
watchdogRead?.();
const repairsAfterWatchdog = repairs;
assert.equal(repair.retry(
  movingPreviewKey, () => { repairs += 1; }, 60_000), false,
  "one fired watchdog is the upper bound for the current failure cycle");
assert.equal(repairs, repairsAfterWatchdog);

const images = new HistoryImageAssetCache(2);
assert.equal(images.begin({
  sid: "session-1", turnId: "turn-1", imageId: "image-1",
  variant: "thumbnail", requestId: "image-request-1", revision: "revision-1",
}), true);
assert.equal(images.accept({
  v: 20, type: "history_image", ts: 1,
  session_id: "session-2", turn_id: "turn-1", image_id: "image-1",
  variant: "thumbnail", request_id: "image-request-1", revision: "revision-1",
  media_type: "image/webp", data: "abc",
}), false, "a delayed response from another session cannot fill this cache");
assert.equal(images.accept({
  v: 20, type: "history_image", ts: 1,
  session_id: "session-1", turn_id: "turn-1", image_id: "image-1",
  variant: "thumbnail", request_id: "image-request-1", revision: "revision-1",
  media_type: "image/webp", width: 10, height: 5, data: "abc",
}), true);
assert.equal(images.forSession("session-1")[
  historyImageAssetKey("turn-1", "image-1", "thumbnail")
].status, "ready");
assert.deepEqual(images.forSession("session-2"), {});
