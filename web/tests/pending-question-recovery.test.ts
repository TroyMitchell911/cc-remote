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
} finally {
  await harness.close();
}
