import assert from "node:assert/strict";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import type { AppState } from "../src/reducer.ts";

const sid = "question-session";
const backgroundSid = "background-session";
const optionList = [{ label: "Yes" }, { label: "No" }];
const wire = (value: object): ServerEvent => ({
  v: 1,
  ts: 1,
  ...value,
}) as unknown as ServerEvent;
const ask = (askId: string): ServerEvent => wire({
  type: "ask_user",
  sid,
  ask_id: askId,
  question: "Current?",
  options: optionList,
  allow_text: false,
  secret: false,
  multi_select: false,
});

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const reducer = (await harness.ssrLoadModule("/src/reducer.ts")) as
    typeof import("../src/reducer.ts");
  const { createRuntime, initialState, reduce } = reducer;
  let state: AppState = {
    ...initialState,
    focusedSid: backgroundSid,
    runtimes: {
      [sid]: createRuntime(),
      [backgroundSid]: createRuntime(),
    },
  };
  state = reduce(state, { type: "event", event: ask("stale") });
  assert.equal(state.focusedSid, backgroundSid,
    "a background question must not steal focus");
  state = reduce(state, { type: "event", event: wire({
    type: "snapshot", sid, cc_session_id: sid, state: "running",
    tail_text: "", generation: "question-generation",
  }) });
  assert.equal(state.runtimes[sid].pendingQuestion?.ask_id, "stale",
    "a generic Snapshot is not a pending-question authority");
  state = reduce(state, { type: "event", event: wire({
    type: "ask_user_sync", sid,
  }) });
  assert.equal(state.runtimes[sid].pendingQuestion, null,
    "the explicit Hello question baseline clears the previous socket");

  state = reduce(state, { type: "event", event: ask("current") });
  state = reduce(state, { type: "event", event: wire({
    type: "replay_start", sid, from_seq: 1, to_seq: 1,
    truncated: false, rebuild: false, generation: "question-generation",
  }) });
  assert.equal(state.runtimes[sid].pendingQuestion?.ask_id, "current",
    "a replay envelope alone cannot clear an active question");
  state = reduce(state, { type: "event", event: wire({
    type: "ask_user_sync", sid,
  }) });
  assert.equal(state.runtimes[sid].pendingQuestion, null,
    "question sync clears before the authoritative ask seed");

  state = reduce(state, { type: "event", event: ask("current") });
  const duplicate = reduce(state, { type: "event", event: ask("current") });
  assert.deepEqual(duplicate.runtimes[sid].pendingQuestion,
    state.runtimes[sid].pendingQuestion,
    "re-seeding the same ask_id is idempotent");
  const staleClose = reduce(duplicate, { type: "event", event: wire({
    type: "ask_user_closed", sid, ask_id: "stale", reason: "superseded",
  }) });
  assert.equal(staleClose.runtimes[sid].pendingQuestion?.ask_id, "current",
    "a close for an older ask_id cannot close the authoritative seed");

  const questions = [{ title: "Where?", options: ["Mac", "Phone"] }];
  let asyncState: AppState = {
    ...initialState, focusedSid: backgroundSid,
    runtimes: { [sid]: createRuntime(), [backgroundSid]: createRuntime() },
  };
  for (const event of [
    { type: "user_msg", msg_id: "async-user", prompt: "work" },
    { type: "state", state: "running" },
    { type: "assistant_msg_start", message_id: "async-item", channel: "final" },
    { type: "delta", message_id: "async-item", text: "Where?", channel: "final" },
    { type: "assistant_msg_end", message_id: "async-item", channel: "final",
      delivery: "async", questions },
    { type: "assistant_msg_end", message_id: "async-item", channel: "final",
      delivery: "async", questions },
  ]) asyncState = reduce(asyncState, { type: "event", event: wire({ ...event, sid }) });
  const runtime = asyncState.runtimes[sid];
  assert.equal(asyncState.focusedSid, backgroundSid);
  assert.equal(runtime.state, "running");
  assert.equal(runtime.pendingQuestion, null, "async does not acquire an approval lease");
  assert.equal(runtime.turns.at(-1)?.done, false, "a completed message is not a completed turn");
  assert.equal(runtime.turns.at(-1)?.blocks.length, 1, "repeat metadata is idempotent");
  const block = runtime.turns.at(-1)!.blocks[0];
  assert.equal(block.kind, "text");
  if (block.kind !== "text") throw new Error("expected text");
  assert.deepEqual(block.questions, questions);
  const { mergeDetailWithLiveTail } = await harness.ssrLoadModule("/src/history-merge.ts") as
    typeof import("../src/history-merge.ts");
  const merged = mergeDetailWithLiveTail([
    { ...block, questions: undefined, delivery: undefined },
  ], [block]);
  assert.equal(merged.length, 1);
  assert.deepEqual(merged[0], block, "native-id detail/live merge restores async metadata");
  const { boundCachedTurns } = await harness.ssrLoadModule("/src/cache.ts") as
    typeof import("../src/cache.ts");
  const cached = boundCachedTurns(runtime.turns) as typeof runtime.turns;
  assert.deepEqual(cached[0].blocks[0], block, "refresh keeps the structured card");
  asyncState = reduce(asyncState, { type: "event", event: wire({
    type: "state", state: "idle", sid,
  }) });
  assert.deepEqual(asyncState.runtimes[sid].turns[0].blocks[0], block,
    "idle does not discard a nonblocking conversation question");
} finally {
  await harness.close();
}
