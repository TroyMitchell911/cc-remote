import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import type { Block, Turn } from "../src/domain/conversation.ts";
import { isComposerBusy } from "../src/composer-submit.ts";
import {
  MAX_BACKGROUND_PROCESS_ITEMS,
  PROTOCOL_VERSION,
  type ServerEvent,
} from "../src/protocol.ts";


const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});

try {
  const { createRuntime, initialState, reduce } =
    await harness.ssrLoadModule("/src/reducer.ts");
  const { ChatView } = await harness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const { assistantContinuations: claudeContinuations } = await harness.ssrLoadModule("/src/assistant-continuations.ts");
  const { mergeInitialHistory, mergeAuthoritativeTurnDetail, restoreCachedTurnDetails } =
    await harness.ssrLoadModule("/src/history-merge.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: PROTOCOL_VERSION,
    ts: 10,
    ...body,
  } as ServerEvent);

  const sid = "claude-background-sync";
  let state = {
    ...initialState,
    focusedSid: sid,
    runtimes: {
      [sid]: { ...createRuntime(), controlGeneration: "generation-1" },
    },
  };
  state = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-1",
    items: [{
      item_id: "bash-task",
      kind: "task",
      status: "running",
      title: "Run verification",
      command: "make verify",
      started_at: 12,
      updated_at: 13,
    }],
  }) });
  assert.equal(state.runtimes[sid].backgroundProcesses[0]?.command,
    "make verify");
  assert.equal(state.runtimes[sid].backgroundProcesses[0]?.startedTs,
    12_000);

  const stale = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-old",
    items: [],
  }) });
  assert.equal(stale.runtimes[sid].backgroundProcesses.length, 1,
    "a delayed old-generation level cannot clear current work");

  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [sid]: {
        ...state.runtimes[sid],
        historyRevision: "background-r1",
        turns: [{
          id: "turn-1",
          prompt: "run verification",
          done: false,
          processDetailState: "present",
          processStartedTs: 12_000,
          blocks: [{
            kind: "process",
            item_id: "bash-task",
            processKind: "task",
            phase: "start",
            status: "running",
            title: "Run verification",
            background: true,
            done: false,
            startedTs: 12_000,
          }],
        }],
      },
    },
  };
  state = reduce(state, { type: "event", event: event({
    type: "background_process_sync",
    sid,
    generation: "generation-1",
    items: [],
  }) });
  assert.equal(state.runtimes[sid].backgroundProcesses.length, 0,
    "the authoritative empty level clears a stale task card");
  const settled = state.runtimes[sid].turns[0].blocks[0];
  assert.equal(settled.kind, "process");
  assert.equal(settled.done, true,
    "the same empty level settles a stale in-turn spinner");
  assert.equal(settled.kind === "process" && settled.status, "unknown",
    "absence proves terminality but must not fabricate success");

  state = reduce(state, { type: "event", event: event({
    type: "turn_detail",
    session_id: sid,
    turn_id: "turn-1",
    revision: "background-r1",
    events: [
      event({ type: "user_msg", sid, msg_id: "turn-1",
        prompt: "run verification" }),
      event({ type: "process", sid, item_id: "late-detail-task",
        kind: "task", phase: "start", status: "running",
        title: "Late cached task", background: true }),
    ],
    has_more: false,
    has_newer: false,
  }) });
  const detailProcess = state.runtimes[sid].turns[0]
    .detailProjection?.blocks.find((block: Block) => block.kind === "process");
  assert.equal(detailProcess?.done, true,
    "detail arriving after the empty level cannot resurrect a ghost task");
  assert.equal(detailProcess?.kind === "process" && detailProcess.status,
    "unknown");

  const cappedSid = "claude-background-cap";
  let cappedState = {
    ...initialState,
    focusedSid: cappedSid,
    runtimes: { [cappedSid]: createRuntime() },
  };
  for (let index = 0; index < MAX_BACKGROUND_PROCESS_ITEMS + 12; index += 1) {
    cappedState = reduce(cappedState, { type: "event", event: event({
      type: "process", sid: cappedSid, item_id: `task-${index}`,
      kind: "task", phase: "start", status: "running",
      title: `Task ${index}`, background: true,
    }) });
  }
  assert.equal(
    cappedState.runtimes[cappedSid].backgroundProcesses.length,
    MAX_BACKGROUND_PROCESS_ITEMS,
    "untrusted edge volume cannot grow the detached-task dock past protocol bounds",
  );

  const followupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "followup-history",
    engine: "claude",
    turns: [{
      id: "turn-1",
      prompt: "run",
      done: true,
      doneTs: 21_000,
      blocks: [
        {
          kind: "text",
          message_id: "before",
          channel: "final",
          text: "Build is running.",
          done: true,
          startedTs: 2_000,
        },
        {
          kind: "process",
          item_id: "task-1",
          processKind: "task",
          phase: "end",
          status: "succeeded",
          title: "Build",
          summary: "Build completed",
          background: true,
          done: true,
          terminalTs: 20_000,
        },
        {
          kind: "text",
          message_id: "after",
          channel: "final",
          text: "Build passed.",
          done: true,
          background: true,
          startedTs: 21_000,
        },
      ],
    }],
  }));
  assert.match(
    followupMarkup,
    /Build is running\.[\s\S]*Claude 继续处理[\s\S]*Build passed\./,
    "a task-completion follow-up keeps its source-time narrative boundary",
  );

  for (const detailState of [
    { detailLoaded: false },
    { detailLoaded: false, detailLoading: true },
    { detailLoaded: false, detailError: "detail unavailable" },
    { detailLoaded: true, detailHasMore: true, detailOldestCursor: "older" },
  ]) {
    const finalOnlyMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: "final-only-continuation", engine: "claude",
      turns: [{
        id: "native-turn", prompt: "run", done: true,
        ts: 1_000, doneTs: 21_000, processDetailState: "present",
        detailEventCount: 28, ...detailState,
        blocks: [
          { kind: "text", message_id: "original", channel: "final",
            text: "Original response.", done: true, startedTs: 2_000 },
          { kind: "text", message_id: "continued-final", channel: "final",
            text: "Completed follow-up.", done: true, background: true,
            startedTs: 20_000, doneTs: 21_000 },
        ],
      }],
    }));
    const continuation = finalOnlyMarkup.slice(finalOnlyMarkup.indexOf("Claude 继续处理"));
    assert.match(continuation, /Completed follow-up\./);
    assert.doesNotMatch(continuation, /已处理|正在处理|process-timeline/,
      "parent detail counts/loading/errors must not create an empty continuation disclosure");
    assert.equal(finalOnlyMarkup.split("Completed follow-up.").length - 1, 1,
      "the real answer remains visible exactly once");
  }

  const processFollowupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "followup-with-process", engine: "claude",
    turns: [{ id: "native-turn", prompt: "run", done: true,
      processDetailState: "present", detailEventCount: 28,
      blocks: [
        { kind: "text", message_id: "original", channel: "final",
          text: "Original response.", done: true },
        { kind: "text", message_id: "continued-thought", channel: "thinking",
          text: "Reviewing the background result.", done: true, background: true },
        { kind: "text", message_id: "continued-final", channel: "final",
          text: "Completed follow-up.", done: true, background: true },
      ],
    }],
  }));
  assert.match(processFollowupMarkup,
    /Claude 继续处理[\s\S]*已处理[\s\S]*1 项[\s\S]*Completed follow-up\./,
    "a continuation with an actual process item retains its own disclosure");

  const concurrentFollowupMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "concurrent-followup-history",
    engine: "claude",
    turns: [{
      id: "turn-concurrent",
      prompt: "run both",
      done: true,
      blocks: [
        { kind: "process", item_id: "task-a", processKind: "task",
          phase: "end", status: "succeeded", title: "Task A",
          summary: "Task A completed", background: true, done: true,
          terminalTs: 20_000 },
        { kind: "text", message_id: "reply-a", channel: "final",
          text: "A report.", done: true, background: true,
          startedTs: 21_000 },
        { kind: "process", item_id: "task-b", processKind: "agent",
          phase: "end", status: "succeeded", title: "Task B",
          summary: "Task B completed", background: true, done: true,
          terminalTs: 30_000 },
        { kind: "text", message_id: "reply-b", channel: "final",
          text: "B report.", done: true, background: true,
          startedTs: 31_000 },
      ],
    }],
  }));
  assert.match(
    concurrentFollowupMarkup,
    /Claude 继续处理[\s\S]*A report\.[\s\S]*Claude 继续处理[\s\S]*B report\./,
    "each detached completion labels its own later reply segment",
  );

  const blocks: Block[] = [
    { kind: "text", message_id: "original", channel: "final", text: "Interim", done: true },
    { kind: "process", item_id: "child", processKind: "agent", phase: "end",
      title: "Long private report stays in child details", status: "succeeded", done: true, background: true },
    { kind: "text", message_id: "thinking-a", channel: "thinking", text: "Fixture thought A", done: true, background: true, startedTs: 20_000 },
    { kind: "tool", message_id: "tool-a-message", tool_use_id: "tool-a", tool: "Bash", input: {}, done: true, background: true },
    { kind: "text", message_id: "answer-a", channel: "final", text: "Result A", done: true, background: true },
    { kind: "text", message_id: "thinking-b", channel: "thinking", text: "", done: false, background: true, startedTs: 30_000 },
  ];
  const narrative = claudeContinuations(blocks, blocks.filter((block) => block.kind === "text" && block.channel === "final"));
  assert.equal(narrative.original.length, 2);
  assert.equal(narrative.continuations.length, 2);
  assert.equal(narrative.continuations[0].blocks.length, 3);
  assert.equal(narrative.continuations[0].answers[0].message_id, "answer-a");
  assert.equal(narrative.continuations[1].id, "thinking-b",
    "a main continuation is visible from its first empty stream frame");

  let continuationState = {
    ...initialState, focusedSid: sid,
    runtimes: { [sid]: { ...createRuntime(), controlGeneration: "generation-1",
      turns: [{ id: "settled-parent", prompt: "run", done: true, blocks: [] }] } },
  };
  const send = (body: Record<string, unknown>) => {
    continuationState = reduce(continuationState, { type: "event", event: event({ sid, ...body }) });
  };
  send({ type: "background_process_sync", generation: "generation-1", items: [{
    item_id: "other-child", kind: "task", status: "running", title: "Background check",
    started_at: 12, updated_at: 13,
  }] });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false,
    "a background child alone must leave the composer available for a normal query");
  send({ type: "state", state: "running" });
  send({ type: "process", turn_id: "settled-parent", item_id: "child", kind: "agent",
    phase: "end", status: "succeeded", title: "Child done", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner, null,
    "a completed child cannot claim the parent's running owner");
  send({ type: "assistant_msg_start", turn_id: "settled-parent", message_id: "continuation", channel: "thinking", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner?.turnId, "settled-parent");
  assert.equal(continuationState.runtimes[sid].turns[0].done, true,
    "the prior native completion receipt remains settled");
  send({ type: "state", state: "idle" });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false,
    "the real main terminal restores normal submission without a hard refresh");
  assert.equal(continuationState.runtimes[sid].backgroundProcesses[0]?.status, "running",
    "settling the main response keeps the still-running child visible");
  send({ type: "delta", turn_id: "settled-parent", message_id: "continuation", channel: "thinking", text: "Late replay", background: true });
  assert.equal(continuationState.runtimes[sid].liveOwner, null,
    "replay into an idle session cannot revive the spark");
  send({ type: "state", state: "running", msg_id: "settled-parent", continuation: true, seq: 20 });
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), true,
    "a later native continuation, rather than the child job, owns steering mode");
  assert.equal(continuationState.runtimes[sid].liveOwner?.turnId, "settled-parent",
    "the native continuation owns its spark before any text or tool output");
  assert.equal(continuationState.runtimes[sid].turns[0].done, true);
  send({ type: "state", state: "idle", seq: 21 });
  assert.equal(continuationState.runtimes[sid].liveOwner, null);
  assert.equal(isComposerBusy(continuationState.runtimes[sid].state), false);

  // Device switching starts with no cached turns. Hello's current owner may
  // arrive before or after History; neither order needs another model token.
  for (const seedFirst of [false, true]) {
    let cold = { ...initialState, focusedSid: sid,
      runtimes: { [sid]: createRuntime() } };
    const seed = event({ type: "state", sid, state: "running",
      msg_id: "browser-parent", continuation: true });
    const history = event({ type: "history", sid, session_id: sid,
      revision: "cold-r1", generation: "generation-1", build_seq: 1,
      live_seq: 100, in_progress: true, detail: "summary", events: [],
      newest_id: "native-parent", has_more: false,
      turns: [{ id: "native-parent", clientMsgId: "browser-parent",
        prompt: "inspect", done: true, blocks: [], detailLoaded: false }] });
    cold = reduce(cold, { type: "event", event: event({ type: "snapshot", sid,
      cc_session_id: sid, state: "running", generation: "generation-1" }) });
    for (const e of seedFirst ? [seed, history] : [history, seed]) {
      cold = reduce(cold, { type: "event", event: e });
    }
    assert.equal(cold.runtimes[sid].liveOwner?.turnId, "native-parent");
    assert.equal(cold.runtimes[sid].turns[0].done, true);
    cold = reduce(cold, { type: "event", event: event({ type: "delta", sid,
      turn_id: "browser-parent", message_id: "unflushed-thinking", seq: 101,
      background: true, channel: "thinking", text: "New work beyond the detail snapshot",
    }) });
    cold = reduce(cold, { type: "event", event: event({ type: "turn_detail", sid,
      session_id: sid, turn_id: "native-parent", revision: "cold-r1",
      has_more: false, has_newer: false, events: [
        event({ type: "user_msg", msg_id: "native-parent", prompt: "inspect" }),
        event({ type: "tool_use", turn_id: "native-parent", tool_use_id: "active-tool",
          message_id: "continuation-tool", tool: "Bash", background: true,
          input: { command: "sleep 1" } }),
      ],
    }) });
    const recovered = cold.runtimes[sid].turns[0];
    assert.equal(recovered.done, true);
    assert.ok(recovered.blocks.some((b: Block) => b.kind === "text"
      && b.message_id === "unflushed-thinking"),
    "a detail page cannot discard newer continuation output on a completed parent");
    assert.equal(recovered.detailProjection.blocks.find((b: Block) =>
      b.kind === "tool" && b.tool_use_id === "active-tool")?.done, false,
    "the parent's old completion must not falsely complete its continuation tool");
    cold = reduce(cold, { type: "event", event: event({
      type: "state", sid, state: "idle", seq: 102,
    }) });
    assert.equal(cold.runtimes[sid].liveOwner, null);
  }

  // An autonomous main response has a new assistant UUID but belongs to the
  // already-completed human row. The binding must work even when streamed
  // envelope IDs differ, and must not touch another row or another engine.
  for (const engine of ["claude", "codex", "dsh"]) {
    let completedState = {
      ...initialState, focusedSid: sid,
      sessions: [{ session_id: sid, engine }],
      runtimes: { [sid]: { ...createRuntime(), controlGeneration: "generation-1",
        turns: [{ id: "parent", prompt: "review", done: true, doneTs: 10_000,
          forkPointId: "original-answer", checkpointId: "native-human", blocks: [],
          error: "Earlier response failed", interrupted: true, terminalSource: "failed" },
        { id: "unrelated", prompt: "other", done: true, doneTs: 11_000,
          forkPointId: "other-answer", blocks: [] }] } },
    };
    let seq = 0;
    const completeSend = (body: Record<string, unknown>) => {
      completedState = reduce(completedState, {
        type: "event", event: event({ sid, seq: ++seq, ...body }),
      });
    };
    completeSend({ type: "state", state: "running", msg_id: "parent", continuation: true });
    completeSend({ type: "delta", turn_id: "parent", message_id: "stream-envelope",
      channel: "final", text: "Review complete.", background: true, ts: 20 });
    completeSend({ type: "assistant_msg_end", turn_id: "parent", message_id: "stream-envelope",
      channel: "final", background: true, ts: 21 });
    completeSend({ type: "turn_binding", msg_id: "parent", turn_id: "followup-answer", ts: 22 });
    assert.equal(completedState.runtimes[sid].turns[0].done, true,
      "binding a continuation must not reopen the original human response");
    completeSend({ type: "turn_end", turn_id: "followup-answer", ts: 22,
      result: { subtype: "success", duration_ms: 2_000, is_error: false },
      notification_context: { engine, space: "code" } });
    completeSend({ type: "state", state: "idle", ts: 22 });
    const [parent, unrelated] = completedState.runtimes[sid].turns;
    assert.equal(completedState.runtimes[sid].turns.length, 2,
      "a continuation completion never adds a synthetic user row");
    assert.equal(parent.doneTs, engine === "claude" ? 22_000 : 10_000);
    assert.equal(parent.forkPointId, engine === "claude" ? "followup-answer" : "original-answer");
    assert.equal(parent.checkpointId, "native-human");
    assert.equal(parent.error, engine === "claude" ? undefined : "Earlier response failed");
    assert.equal(parent.interrupted, engine === "claude" ? undefined : true);
    assert.equal(parent.terminalSource, engine === "claude" ? undefined : "failed");
    assert.equal(unrelated.doneTs, 11_000);
    if (engine === "claude") {
      completeSend({ type: "completion_state", completion_id: "followup-answer",
        unread: true, revision: 2 });
      assert.equal(completedState.runtimes[sid].completion.id, "followup-answer");
      assert.equal(completedState.runtimes[sid].completion.unread, true);
    }
  }

  // A provider failure and its later native continuation share a human row,
  // but not a terminal outcome. Exercise live, cache, detail and rendered order.
  const failedAnswer: Block = { kind: "text", message_id: "api-error", channel: "final",
    text: "API Error: 400 upstream refused the request", done: true };
  const recoveryWork: Block = { kind: "tool", message_id: "recovery-work", tool_use_id: "recheck",
    tool: "Bash", input: { command: "check" }, background: true, done: true,
    startedTs: 20_000, doneTs: 21_000, result: { content: "ok", is_error: false } };
  const recoveryAnswer: Block = { kind: "text", message_id: "recovered", channel: "final",
    text: "Recovered response complete.", background: true, done: true,
    startedTs: 22_000, doneTs: 23_000 };
  const failedParent: Turn = { id: "human", checkpointId: "native-human", prompt: "check",
    done: true, doneTs: 10_000, error: "Earlier response failed", terminalSource: "failed",
    forkPointId: "api-error", blocks: [failedAnswer] };
  for (const subtype of ["error", "error_during_execution"]) {
    let failureState = { ...initialState, focusedSid: sid,
      sessions: [{ session_id: sid, engine: "claude" }],
      runtimes: { [sid]: { ...createRuntime(), turns: [failedParent] } } };
    for (const body of [
      { type: "state", state: "running", msg_id: "human", continuation: true, seq: 1 },
      { type: "turn_binding", msg_id: "human", turn_id: "new-failure", seq: 2 },
      { type: "turn_end", turn_id: "new-failure", seq: 3, ts: 23,
        result: { subtype, duration_ms: 3_000, is_error: true },
        notification_context: { engine: "claude", space: "code" } },
    ]) failureState = reduce(failureState, { type: "event", event: event({ sid, ...body }) });
    const terminal = failureState.runtimes[sid].turns[0];
    assert.equal(terminal.doneTs, 23_000);
    assert.equal(terminal.forkPointId, "new-failure");
    assert.equal(terminal.error, subtype === "error" ? "本次回复未完成，请重试。" : undefined);
    assert.equal(terminal.interrupted, subtype === "error_during_execution" ? true : undefined);
    assert.equal(terminal.terminalSource, subtype === "error" ? "failed" : "unexpected_interrupt");
  }
  const successfulHistory: Turn = { ...failedParent, doneTs: 23_000, forkPointId: "recovered",
    error: undefined, terminalSource: undefined, blocks: [failedAnswer, recoveryWork, recoveryAnswer] };
  const staleCache: Turn = { ...failedParent, doneTs: 23_000, forkPointId: "recovered",
    blocks: [failedAnswer, recoveryWork, { ...recoveryAnswer, background: undefined }] };
  const refreshed = reduce({ ...initialState, focusedSid: sid,
    sessions: [{ session_id: sid, engine: "claude" }],
    runtimes: { [sid]: { ...createRuntime(), turns: [staleCache] } },
  }, { type: "event", event: event({ type: "history", sid, session_id: sid,
    revision: "continuation-repaired", detail: "summary", in_progress: false,
    has_more: false, events: [], turns: [successfulHistory] }) });
  for (const merged of [
    refreshed.runtimes[sid].turns[0],
    mergeInitialHistory([successfulHistory], [staleCache])[0],
    mergeInitialHistory([staleCache], [successfulHistory])[0],
    mergeAuthoritativeTurnDetail(successfulHistory, staleCache),
  ]) {
    assert.equal(merged.error, undefined, "old failed cache cannot override a successful native continuation");
    assert.equal(merged.terminalSource, undefined);
    const markup = renderToStaticMarkup(createElement(ChatView, {
      sid, engine: "claude", turns: [merged],
    }));
    assert.match(markup, /API Error:[\s\S]*Claude 继续处理[\s\S]*已处理[\s\S]*Recovered response complete\./);
    assert.doesNotMatch(markup, /回复未完成/);
  }
  for (const history of [
    { ...successfulHistory, done: false },
    { ...successfulHistory, checkpointId: undefined },
    { ...successfulHistory, doneTs: 9_000 },
    { ...successfulHistory, forkPointId: "unproven-final" },
    { ...successfulHistory, blocks: [failedAnswer, recoveryWork] },
  ]) {
    assert.equal(mergeInitialHistory([history], [failedParent])[0].error, failedParent.error,
      "a receipt, process-only snapshot, other engine or older terminal cannot erase a failure");
  }
  const legacyHistory = { ...successfulHistory, blocks: [failedAnswer,
    { ...recoveryAnswer, background: undefined, startedTs: undefined, doneTs: undefined }] };
  const withScope = mergeInitialHistory([legacyHistory], [successfulHistory])[0];
  const retainedAnswer = withScope.blocks.find((block: Block) =>
    block.kind === "text" && block.message_id === "recovered");
  assert.equal(retainedAnswer.background, true);
  assert.equal(retainedAnswer.startedTs, 22_000);
  assert.equal(retainedAnswer.doneTs, 23_000);

  const laterWork: Block = { ...recoveryWork, message_id: "later-work", tool_use_id: "later-check",
    startedTs: 30_000, doneTs: 31_000 };
  const laterFailed: Turn = { ...successfulHistory, doneTs: 32_000, forkPointId: "later-error",
    error: "Later response failed", terminalSource: "failed",
    blocks: [...successfulHistory.blocks, laterWork, { ...failedAnswer, message_id: "later-error",
      background: true, startedTs: 32_000, doneTs: 32_000 }] };
  const failedMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, engine: "claude", turns: [laterFailed],
  })).split("Claude 继续处理");
  assert.doesNotMatch(failedMarkup[1], /回复未完成/,
    "a later failure cannot mark an earlier successful continuation failed");
  assert.match(failedMarkup[2], /回复未完成/);
  assert.equal(mergeInitialHistory([successfulHistory], [laterFailed])[0].error, laterFailed.error);
  const runningMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, engine: "claude", activeTurnId: "human", activePhase: "process",
    turns: [{ ...failedParent, blocks: [failedAnswer, { ...recoveryWork, done: false, result: undefined }] }],
  })).split("Claude 继续处理")[1];
  assert.match(runningMarkup, /正在处理/);
  assert.doesNotMatch(runningMarkup, /回复未完成/,
    "the earlier terminal cannot suppress a genuinely active native continuation");

  // A resident Claude can continue a settled turn after the browser has only
  // loaded its lightweight summary. A subsequent summary omits the Bash item,
  // but must not move that already-observed work behind its own final answer.
  const summarySid = "claude-summary-continuation";
  const originalAnswer = {
    kind: "text", message_id: "original-answer", channel: "final",
    text: "Waiting for CI.", done: true, startedTs: 2_000,
  };
  const continuedAnswer = {
    kind: "text", message_id: "continued-answer", channel: "final",
    text: "CI passed.", done: true, background: true,
    startedTs: 30_000, doneTs: 33_000,
  };
  let summaryState = {
    ...initialState, focusedSid: summarySid,
    sessions: [{ session_id: summarySid, engine: "claude" }],
    runtimes: { [summarySid]: createRuntime() },
  };
  const summarySend = (body: Record<string, unknown>) => {
    summaryState = reduce(summaryState, { type: "event", event: event({
      sid: summarySid, ...body,
    }) });
  };
  const refreshSummary = (continued: boolean) => summarySend({
    type: "history", session_id: summarySid, revision: "summary-r1",
    detail: "summary", in_progress: false, has_more: true, events: [],
    turns: [{ id: "summary-parent", prompt: "check CI", done: true,
      ts: 1_000, doneTs: 3_000, processDetailState: "present",
      blocks: continued ? [originalAnswer, continuedAnswer] : [originalAnswer] }],
  });
  refreshSummary(false);
  summarySend({ type: "state", state: "running", msg_id: "summary-parent", continuation: true });
  summarySend({ type: "assistant_msg_start", turn_id: "summary-parent",
    message_id: "check-message", channel: "commentary", background: true, ts: 28 });
  summarySend({ type: "tool_use", turn_id: "summary-parent", message_id: "check-message",
    tool_use_id: "check-ci", tool: "Bash", input: { command: "check CI" }, background: true, ts: 28 });
  summarySend({ type: "assistant_msg_end", turn_id: "summary-parent",
    message_id: "check-message", channel: "commentary", background: true, ts: 28 });
  summarySend({ type: "tool_result", turn_id: "summary-parent", tool_use_id: "check-ci",
    content: "passed", is_error: false, background: true, ts: 29 });
  summarySend({ type: "assistant_msg_start", turn_id: "summary-parent",
    message_id: "continued-answer", channel: "final", background: true, ts: 30 });
  summarySend({ type: "delta", turn_id: "summary-parent", message_id: "continued-answer",
    channel: "final", text: "CI passed.", background: true, ts: 31 });
  summarySend({ type: "assistant_msg_end", turn_id: "summary-parent",
    message_id: "continued-answer", channel: "final", background: true, ts: 33 });
  summarySend({ type: "state", state: "idle", ts: 33 });
  const summaryMarkup = () => renderToStaticMarkup(createElement(ChatView, {
    sid: summarySid, engine: "claude", turns: summaryState.runtimes[summarySid].turns,
  }));
  assert.equal(summaryMarkup().split("Claude 继续处理").length - 1, 1);
  refreshSummary(true);
  assert.equal(summaryMarkup().split("Claude 继续处理").length - 1, 1,
    "a summary refresh keeps the observed tool before its continuation answer");
  assert.match(summaryMarkup(), /Claude 继续处理[\s\S]*已处理[\s\S]*CI passed\./);
  assert.equal(summaryState.runtimes[summarySid].turns[0].done, true);
  assert.equal(summaryState.runtimes[summarySid].liveOwner, null,
    "repairing presentation order does not reopen the completed parent");

  summarySend({ type: "state", state: "running", msg_id: "summary-parent", continuation: true });
  summarySend({ type: "assistant_msg_start", turn_id: "summary-parent",
    message_id: "next-thought", channel: "thinking", background: true, ts: 40 });
  assert.equal(summaryMarkup().split("Claude 继续处理").length - 1, 2,
    "a genuinely new continuation still appears immediately, even before text arrives");
  summarySend({ type: "delta", turn_id: "summary-parent", message_id: "next-thought",
    channel: "thinking", text: "Checking the next task.", background: true, ts: 41 });
  summarySend({ type: "assistant_msg_end", turn_id: "summary-parent",
    message_id: "next-thought", channel: "thinking", background: true, ts: 42 });
  summarySend({ type: "assistant_msg_start", turn_id: "summary-parent",
    message_id: "next-answer", channel: "final", background: true, ts: 43 });
  summarySend({ type: "delta", turn_id: "summary-parent", message_id: "next-answer",
    channel: "final", text: "Next task passed.", background: true, ts: 44 });
  summarySend({ type: "assistant_msg_end", turn_id: "summary-parent",
    message_id: "next-answer", channel: "final", background: true, ts: 45 });
  summarySend({ type: "state", state: "idle", ts: 45 });
  assert.equal(summaryMarkup().split("Claude 继续处理").length - 1, 2);
  assert.match(summaryMarkup(), /CI passed\.[\s\S]*Claude 继续处理[\s\S]*Next task passed\./);

  // With no summary loaded before the background response, the completed live
  // row still holds all process blocks inline. A later summary must weave those
  // blocks before their exact final-answer anchor, even without an active owner.
  const completedBlocks: Block[] = [{
    kind: "text", message_id: "settled-original", channel: "final",
    text: "Waiting for results.", done: true, liveOrder: 0,
  }, {
    kind: "text", message_id: "settled-thinking", channel: "thinking",
    text: "Inspect the results.", done: true, background: true,
    startedTs: 10_000, liveOrder: 1,
  }, {
    kind: "tool", message_id: "settled-command", tool_use_id: "settled-read",
    tool: "Bash", category: "command", input: { command: "cat result.txt" },
    result: { content: "passed", is_error: false },
    done: true, background: true, liveOrder: 2,
  }, {
    kind: "text", message_id: "settled-final", channel: "final",
    text: "The check passed.", done: true, background: true,
    startedTs: 20_000, doneTs: 21_000, liveOrder: 3,
  }];
  const completedLive: Turn = {
    id: "settled-background-parent", prompt: "Run the check", done: true,
    blocks: completedBlocks,
  };
  const completedSummary: Turn = {
    ...completedLive,
    processDetailState: "present", detailEventCount: 3, detailLoaded: false,
    blocks: completedBlocks.filter(block =>
      block.kind === "text" && block.channel === "final")
      .map(block => ({ ...block, liveOrder: undefined })),
  };
  const identity = (block: Block) => block.kind === "text" ? block.message_id
    : block.kind === "tool" ? block.tool_use_id : block.item_id;
  const continuations = (turn: Turn) => claudeContinuations(turn.blocks,
    turn.blocks.filter(block => block.kind === "text" && block.channel === "final"),
    "claude").continuations;
  let completedMerged = completedLive;
  for (let refresh = 0; refresh < 3; refresh += 1) {
    completedMerged = mergeInitialHistory([completedSummary], [completedMerged], {
      reconcileReplayOrphans: true, newestHistoryId: completedSummary.id,
    })[0];
    assert.deepEqual(completedMerged.blocks.map(identity), completedBlocks.map(identity),
      "refreshing a settled row preserves witnessed process order around the summary's exact answer");
    const replies = continuations(completedMerged);
    assert.equal(replies.length, 1,
      "a completed process must not become an empty second continuation after its answer");
    assert.equal(replies[0].startedTs, 10_000);
    assert.deepEqual(replies[0].answers.map(identity), ["settled-final"]);
    assert.equal(completedMerged.done, true);
  }
  const completedMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "settled-background", engine: "claude", turns: [completedMerged],
  }));
  assert.equal(completedMarkup.split("Claude 继续处理").length - 1, 1);
  assert.match(completedMarkup,
    /Waiting for results\.[\s\S]*Claude 继续处理[\s\S]*已处理[\s\S]*The check passed\./);
  const cachedCompleted = restoreCachedTurnDetails([completedSummary],
    JSON.parse(JSON.stringify([completedMerged])), "idle");
  const cachedMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "settled-background", engine: "claude", turns: cachedCompleted,
  }));
  assert.equal(cachedMarkup.split("Claude 继续处理").length - 1, 1,
    "restoring the completed process from cache must keep it with its answer");
  assert.match(cachedMarkup,
    /Claude 继续处理[\s\S]*已处理[\s\S]*The check passed\./);

  const secondReplyBlocks: Block[] = [{
    kind: "text", message_id: "settled-next-thought", channel: "thinking",
    text: "Inspect the next result.", done: true, background: true,
    startedTs: 30_000, liveOrder: 4,
  }, {
    kind: "text", message_id: "settled-next-final", channel: "final",
    text: "The next check passed.", done: true, background: true,
    startedTs: 40_000, liveOrder: 5,
  }];
  const twiceCompleted = mergeInitialHistory([{
    ...completedSummary,
    blocks: [...completedSummary.blocks, { ...secondReplyBlocks[1], liveOrder: undefined }],
  }], [{ ...completedMerged, blocks: [...completedMerged.blocks, ...secondReplyBlocks] }])[0];
  assert.deepEqual(continuations(twiceCompleted).map((reply: { answers: Block[] }) =>
    reply.answers.map(identity)), [["settled-final"], ["settled-next-final"]],
  "two completed background responses retain two distinct answer/process groups");

  for (const unprovenBlocks of [
    completedBlocks.map(block => ({ ...block, liveOrder: undefined })),
    completedBlocks.map(block => ({ ...block, liveOrder: 0 })),
    completedBlocks.map((block, index) => ({ ...block, liveOrder: 3 - index })),
  ]) {
    const unproven = mergeInitialHistory([completedSummary], [{
      ...completedLive, blocks: unprovenBlocks,
    }])[0];
    assert.deepEqual(unproven.blocks.slice(0, 2).map(identity),
      completedSummary.blocks.map(identity),
      "missing, duplicate or conflicting order cannot override the canonical answer sequence");
    assert.deepEqual(new Set(unproven.blocks.map(identity)), new Set(completedBlocks.map(identity)),
      "an unproven ordering never discards retained process or answer payloads");
  }

  // The idle summary moves observed process into detailProjection and drops
  // the final's liveOrder. A later continuation must share one order with that
  // retained prefix, including when native transcript IDs replace stream IDs.
  for (const nativeFinalId of ["followup-answer", "transcript-answer"]) {
    const detailSid = `claude-retained-detail-${nativeFinalId}`;
    let detailState = {
      ...initialState, focusedSid: detailSid,
      sessions: [{ session_id: detailSid, engine: "claude" }],
      runtimes: { [detailSid]: createRuntime() },
    };
    const detailSend = (body: Record<string, unknown>) => {
      detailState = reduce(detailState, { type: "event", event: event({
        sid: detailSid, ...body,
      }) });
    };
    const text = (id: string, channel: string, body: string, background = false) => {
      const common = { turn_id: "detail-parent", message_id: id, background };
      detailSend({ type: "assistant_msg_start", ...common, channel: "unknown" });
      detailSend({ type: "delta", ...common, channel, text: body });
      detailSend({ type: "assistant_msg_end", ...common, channel });
    };
    const tool = (id: string, name: string, background = false) => {
      const common = { turn_id: "detail-parent", tool_use_id: id, background };
      detailSend({ type: "tool_use", ...common,
        message_id: `${id}-message`, tool: name, input: {} });
      detailSend({ type: "tool_result", ...common, content: "ok", is_error: false });
    };
    const refresh = (continued = false) => detailSend({
      type: "history", session_id: detailSid, revision: "retained-detail-r1",
      detail: "summary", in_progress: false, has_more: true, events: [],
      turns: [{ id: "detail-parent", prompt: "check CI", done: true,
        processDetailState: "present", detailEventCount: 5,
        blocks: [
          { kind: "text", message_id: "parent-answer", channel: "final",
            text: "Waiting for CI.", done: true },
          ...(continued ? [{ kind: "text", message_id: nativeFinalId,
            channel: "final", text: "The watcher was restarted.", done: true,
            background: true }] : []),
        ] }],
    });
    detailSend({ type: "user_msg", msg_id: "detail-parent", prompt: "check CI" });
    tool("start-watcher", "Bash");
    text("parent-answer", "final", "Waiting for CI.");
    detailSend({ type: "turn_end", result: {
      subtype: "success", duration_ms: 1000, is_error: false,
    } });
    refresh();
    const beforeContinuation = detailState;
    const retained = detailState.runtimes[detailSid].turns[0];
    assert.equal(retained.blocks.length, 1);
    assert.equal(retained.detailProjection?.blocks[0].tool_use_id, "start-watcher");
    const retainedSnapshot = JSON.stringify(retained);

    detailSend({ type: "state", state: "running", msg_id: "detail-parent",
      continuation: true });
    tool("read-result", "Read", true);
    text("followup-thought", "thinking", "Inspect the result.", true);
    text("followup-comment", "commentary", "Restart the watcher.", true);
    tool("restart-watcher", "Bash", true);
    text("followup-answer", "final", "The watcher was restarted.", true);
    detailSend({ type: "state", state: "idle" });
    refresh(true);

    const turn = detailState.runtimes[detailSid].turns[0];
    const markup = renderToStaticMarkup(createElement(ChatView, {
      sid: detailSid, engine: "claude", turns: [turn],
    }));
    assert.equal(markup.split("Claude 继续处理").length - 1, 1,
      "retained parent detail cannot split the follow-up answer from its four process items");
    assert.match(markup,
      /Waiting for CI\.[\s\S]*Claude 继续处理[\s\S]*4 项[\s\S]*The watcher was restarted\./);
    assert.equal(markup.split("The watcher was restarted.").length - 1, 1);
    assert.equal(turn.done, true);
    assert.equal(detailState.runtimes[detailSid].liveOwner, null);
    assert.equal(JSON.stringify(beforeContinuation.runtimes[detailSid].turns[0]),
      retainedSnapshot, "ordering repair must not mutate the prior reducer state");

    detailSend({ type: "state", state: "running", msg_id: "detail-parent",
      continuation: true });
    tool("next-task-result", "Read", true);
    const nextMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: detailSid, engine: "claude", turns: detailState.runtimes[detailSid].turns,
    }));
    assert.equal(nextMarkup.split("Claude 继续处理").length - 1, 2,
      "a real later continuation remains distinct from the repaired response");
    assert.match(nextMarkup,
      /The watcher was restarted\.[\s\S]*Claude 继续处理/);
  }
} finally {
  await harness.close();
}
