import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  installAuthoritativeTurnDetailPage,
  mergeAuthoritativeTurnDetail,
  mergeInitialHistory,
  restoreCachedTurnDetails,
} from "../src/history-merge.ts";
import type { ServerEvent } from "../src/protocol.ts";
import type { Turn } from "../src/reducer.ts";
import {
  acceptAgentDetail,
  emptyAgentRun,
  projectAgentEvents,
} from "../src/agent-detail.ts";
import type { AgentDetail } from "../src/protocol.ts";

const agentEvents: ServerEvent[] = [{
  v: 37, type: "process", item_id: "nested-agent", kind: "agent",
  phase: "start", status: "running", title: "检查测试", background: true,
  ts: 1,
}];
const agentBlocks = projectAgentEvents(agentEvents);
assert.equal(agentBlocks.length, 1);
assert.equal(agentBlocks[0]?.kind, "process");

const initialAgentDetail: AgentDetail = {
  v: 37, type: "agent_detail", session_id: "session", run_id: "agent-run",
  request_id: "request", revision: "history-revision",
  detail_revision: "agent-revision", authoritative: true,
  title: "审查后端", status: "running", events: agentEvents,
  through_seq: 1, has_more: false, ts: 1,
};
const acceptedAgent = acceptAgentDetail(
  { ...emptyAgentRun("agent-run"), requestId: "request" },
  initialAgentDetail,
);
assert.equal(acceptedAgent.blocks.length, 1);
assert.equal(acceptedAgent.loading, false);
const duplicateLive = acceptAgentDetail(acceptedAgent, {
  ...initialAgentDetail, request_id: null, live: true,
  authoritative: false, events: agentEvents, through_seq: 1,
});
assert.equal(duplicateLive.events.length, 1,
  "a repeated live watermark must not duplicate Agent events");
assert.equal(duplicateLive.detailRevision, "agent-revision",
  "live status-only updates retain the current resident detail revision");
const advancedLive = acceptAgentDetail(duplicateLive, {
  ...initialAgentDetail, request_id: null, live: true,
  authoritative: false, events: [], through_seq: 2,
  detail_revision: "agent-revision-2", status: "succeeded",
});
assert.equal(advancedLive.detailRevision, "agent-revision-2");
assert.equal(advancedLive.status, "succeeded");

const opaqueDirectSummary: Turn = {
  id: "opaque-direct", prompt: "hello", done: true,
  processDetailState: "unknown", detailReasons: [],
  detailEventCount: 0, detailLoaded: false,
  blocks: [{
    kind: "text", message_id: "opaque-direct-final", channel: "final",
    text: "hi", done: true,
  }],
};
const exactDirectDetail: Turn = {
  ...opaqueDirectSummary,
  processDetailState: "none",
  detailLoaded: true,
};
const refinedDirect = mergeAuthoritativeTurnDetail(
  opaqueDirectSummary, exactDirectDetail);
assert.equal(refinedDirect.processDetailState, "none",
  "authoritative final-only detail refines an opaque summary to direct reply");
const directAfterOpaqueRefresh = mergeInitialHistory(
  [opaqueDirectSummary], [refinedDirect])[0];
assert.equal(directAfterOpaqueRefresh.processDetailState, "none",
  "same-revision opaque refresh cannot resurrect a dismissed process shell");

const stableKnownProcess = mergeAuthoritativeTurnDetail({
  ...opaqueDirectSummary,
  id: "known-process",
  processDetailState: "present",
  detailReasons: ["process"],
  detailEventCount: 4,
}, {
  ...exactDirectDetail,
  id: "known-process",
});
assert.equal(stableKnownProcess.processDetailState, "present",
  "a final-only replacement page cannot erase known process presence");

const stableLiveKnownProcess = mergeInitialHistory([{
  ...exactDirectDetail,
  id: "live-known-process",
}], [{
  ...exactDirectDetail,
  id: "live-known-process",
  processDetailState: "present",
  detailReasons: ["process"],
  detailLoaded: false,
}])[0];
assert.equal(stableLiveKnownProcess.processDetailState, "present",
  "an exact earlier snapshot cannot erase later same-revision process evidence");

const sourceTimedProcess: Turn = {
  ...exactDirectDetail,
  id: "source-timed-process",
  processDetailState: "present",
  detailReasons: ["process"],
  processStartedTs: 10_000,
  processDoneTs: 14_000,
};
const processAfterStaleZeroCache = mergeInitialHistory(
  [sourceTimedProcess],
  [{
    ...sourceTimedProcess,
    processStartedTs: 90_000,
    processDoneTs: 90_000,
  }],
)[0];
assert.equal(processAfterStaleZeroCache.processStartedTs, 10_000);
assert.equal(processAfterStaleZeroCache.processDoneTs, 14_000,
  "a stale parser-time zero interval cannot stretch source-backed timing");

const partialDirectDetail = installAuthoritativeTurnDetailPage(
  { ...opaqueDirectSummary, id: "partial-direct" },
  { ...exactDirectDetail, id: "partial-direct" },
  {
    hasMore: true,
    oldestCursor: "older-process-page",
    hasNewer: false,
  },
);
assert.equal(partialDirectDetail.processDetailState, "unknown",
  "a final-only bounded page cannot prove unread older pages have no process");
assert.equal(partialDirectDetail.detailLoaded, false);
assert.equal(partialDirectDetail.detailRetryDirection, "older");
assert.equal(partialDirectDetail.detailRetryBefore, "older-process-page");

const legacyUnknownEnvelope = mergeInitialHistory([{
  ...exactDirectDetail,
  id: "legacy-unknown-envelope",
  blocks: [{
    kind: "text", message_id: "legacy-unknown-text", channel: "unknown",
    text: "compatibility answer", done: true,
  }],
}], [{
  ...exactDirectDetail,
  id: "legacy-unknown-envelope",
  blocks: [],
}])[0];
assert.equal(legacyUnknownEnvelope.processDetailState, "none",
  "an unclassified compatibility answer cannot manufacture a process shell");

const restoredUnknownProcess = restoreCachedTurnDetails([{
  ...opaqueDirectSummary,
  id: "restored-unknown-process",
}], [{
  ...opaqueDirectSummary,
  id: "restored-unknown-process",
  processDetailState: "present",
  detailReasons: ["process"],
  blocks: [{
    kind: "tool", message_id: "restored-tool-message",
    tool_use_id: "restored-tool", tool: "Read", input: {}, done: true,
  }],
}], "provisional")[0];
assert.equal(restoredUnknownProcess.processDetailState, "present");
assert.equal(restoredUnknownProcess.detailProjection?.blocks.length, 1,
  "an opaque zero-count summary restores same-revision cached process");

const staleLoadedUnknown = mergeInitialHistory([{
  ...opaqueDirectSummary,
  id: "stale-loaded-unknown",
}], [{
  ...opaqueDirectSummary,
  id: "stale-loaded-unknown",
  detailLoaded: true,
}])[0];
assert.equal(staleLoadedUnknown.detailLoaded, false,
  "a stale loaded bit cannot suppress opaque zero-count detail");

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
  const { ProcessTimeline } = await harness.ssrLoadModule(
    "/src/components/ProcessTimeline.tsx");
  const { BtwPanel } = await harness.ssrLoadModule(
    "/src/components/BtwPanel.tsx");
  const { AgentDetailPanel } = await harness.ssrLoadModule(
    "/src/components/AgentDetailPanel.tsx");
  const { activeTurnCandidateIds, displayActiveTurnOwnerId } =
    await harness.ssrLoadModule("/src/process-blocks.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 37, ts: 10, ...body,
  } as ServerEvent);

  const agentPanelMarkup = renderToStaticMarkup(createElement(
    AgentDetailPanel,
    {
      run: acceptedAgent,
      canGoBack: false,
      onBack: () => {},
      onClose: () => {},
      onRetry: () => {},
      onLoadEarlier: () => {},
      onOpenAgent: () => {},
    },
  ));
  assert.match(agentPanelMarkup,
    /aria-label="调整协作代理面板宽度"/);
  assert.match(agentPanelMarkup, /data-lock-horizontal-swipe="true"/);

  const btwRuntime = createRuntime();
  btwRuntime.state = "idle";
  btwRuntime.syncReady = true;
  btwRuntime.liveOwner = { turnId: "btw-previous-turn", seq: 8 };
  btwRuntime.acceptancePending = "btw-new-turn";
  btwRuntime.turns = [{
    id: "btw-previous-turn", prompt: "previous prompt", done: true,
    blocks: [],
  }, {
    id: "btw-new-turn", prompt: "new prompt", done: false, blocks: [],
  }];
  const btwMarkup = renderToStaticMarkup(createElement(BtwPanel, {
    sid: "btw-session", rt: btwRuntime, engine: "codex", opening: false,
    active: "btw", hasArtifact: false, catalog: {},
    draftKey: "btw-session-draft",
    draftStore: {
      get: () => ({ input: "", images: [], files: [], pastes: [] }),
      set: () => {},
    },
    sendMode: "steer", unconfirmedQueued: [], unconfirmedReplaceable: [],
    queueCapacity: {}, replaceQueueCapacity: {}, onTab: () => {},
    onSend: () => true, onSteer: () => true, onInterrupt: () => {},
    onSetSendMode: () => {}, onEnqueue: () => true,
    onSetPending: () => true, onRemoveQueued: () => {},
    onInspectQueued: () => {}, onSetModel: () => {}, onSetEffort: () => {},
    onClose: () => {}, onDismissNotice: () => {},
  }));
  assert.equal((btwMarkup.match(/思考中/g) ?? []).length, 1);
  assert.ok(btwMarkup.indexOf("思考中") > btwMarkup.indexOf("new prompt"),
    "BTW paints the pending submit spark on the new row, not its stale owner");

  const claudeEchoGapSid = "claude-user-echo-keeps-working-owner";
  const claudeEchoGapMessage = "claude-browser-message";
  let claudeEchoGapState = reduce({
    ...initialState,
    focusedSid: claudeEchoGapSid,
    runtimes: {
      [claudeEchoGapSid]: {
        ...createRuntime(), state: "idle" as const, syncReady: true,
        turns: [{
          id: "claude-previous-turn", prompt: "previous", blocks: [],
          done: true,
        }],
        liveOwner: { turnId: "claude-previous-turn", seq: 10 },
      },
    },
  }, {
    type: "query_sent", sid: claudeEchoGapSid,
    prompt: "keep the spark visible", msg_id: claudeEchoGapMessage,
    ts: 10_000,
  });
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "state", sid: claudeEchoGapSid, seq: 11, state: "running",
    }),
  });
  const activeOwner = () => {
    const runtime = claudeEchoGapState.runtimes[claudeEchoGapSid];
    return activeTurnCandidateIds(
      runtime.turns,
      displayActiveTurnOwnerId(
        runtime.liveOwner?.turnId, runtime.acceptancePending),
      runtime.state !== "idle" || !!runtime.acceptancePending,
    );
  };
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the optimistic Claude submit owns the working spark before its echo");
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "user_msg", sid: claudeEchoGapSid, seq: 12,
      msg_id: claudeEchoGapMessage, prompt: "keep the spark visible",
    }),
  });
  assert.equal(
    claudeEchoGapState.runtimes[claudeEchoGapSid].acceptancePending,
    null,
    "the authoritative Claude user echo still releases the submit latch",
  );
  assert.equal(
    claudeEchoGapState.runtimes[claudeEchoGapSid].liveOwner?.turnId,
    claudeEchoGapMessage,
    "the same echo atomically transfers display ownership to the accepted row",
  );
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the spark stays visible while Claude's TurnBinding is pending");
  claudeEchoGapState = reduce(claudeEchoGapState, {
    type: "event", event: event({
      type: "turn_binding", sid: claudeEchoGapSid, seq: 13,
      msg_id: claudeEchoGapMessage, turn_id: "claude-native-user",
    }),
  });
  assert.deepEqual(activeOwner(), [claudeEchoGapMessage],
    "the later native binding keeps the visible Claude owner stable");

  const claudeHistoryGapSid = "claude-history-acceptance-keeps-owner";
  const claudeHistoryGapMessage = "claude-history-browser-message";
  const claudeHistoryGapNative = "claude-history-native-message";
  const claudeHistoryGapGeneration = "claude-history-generation";
  let claudeHistoryGapBase = reduce({
    ...initialState,
    focusedSid: claudeHistoryGapSid,
    runtimes: {
      [claudeHistoryGapSid]: {
        ...createRuntime(), state: "idle" as const, syncReady: true,
        controlGeneration: claudeHistoryGapGeneration,
        historyGeneration: claudeHistoryGapGeneration,
        historyRevision: "claude-history-revision",
        historyBuildSeq: 1,
        historyLiveSeq: 10,
        historyNewestId: "claude-history-previous",
        liveOwner: { turnId: "claude-history-previous", seq: 10 },
        turns: [{
          id: "claude-history-previous", prompt: "previous", blocks: [],
          done: true,
        }],
      },
    },
  }, {
    type: "query_sent", sid: claudeHistoryGapSid,
    prompt: "survive a lost user echo", msg_id: claudeHistoryGapMessage,
    ts: 20_000,
  });
  claudeHistoryGapBase = reduce(claudeHistoryGapBase, {
    type: "event", event: event({
      type: "state", sid: claudeHistoryGapSid, seq: 11, state: "running",
    }),
  });
  const acceptedHistoryTurn = {
    id: claudeHistoryGapNative,
    clientMsgId: claudeHistoryGapMessage,
    prompt: "survive a lost user echo",
    done: false,
    blocks: [],
    detailEventCount: 0,
    detailLoaded: false,
  };
  const historyOnlyAcceptance = (
    overrides: Record<string, unknown> = {},
  ): ServerEvent => event({
    type: "history", session_id: claudeHistoryGapSid,
    revision: "claude-history-revision",
    generation: claudeHistoryGapGeneration,
    build_seq: 2, live_seq: 12,
    detail: "summary", authoritative: true,
    in_progress: true, external: false, has_more: false,
    newest_id: claudeHistoryGapNative,
    events: [], turns: [acceptedHistoryTurn],
    ...overrides,
  });
  const activeHistoryOwner = (runtime: ReturnType<typeof createRuntime>) =>
    activeTurnCandidateIds(
      runtime.turns,
      displayActiveTurnOwnerId(
        runtime.liveOwner?.turnId, runtime.acceptancePending),
      runtime.state !== "idle" || runtime.mirroredRunning
        || !!runtime.acceptancePending,
    );
  let claudeHistoryGapState = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance(),
  });
  let claudeHistoryGapRuntime =
    claudeHistoryGapState.runtimes[claudeHistoryGapSid];
  assert.equal(claudeHistoryGapRuntime.acceptancePending, null,
    "authoritative History confirms the optimistic submit without its live echo");
  assert.deepEqual(claudeHistoryGapRuntime.liveOwner, {
    turnId: claudeHistoryGapMessage, seq: 12,
  }, "current running History transfers ownership to its exact accepted head");
  assert.deepEqual(activeHistoryOwner(claudeHistoryGapRuntime),
    [claudeHistoryGapMessage],
    "History-only acceptance keeps the working spark on the submitted row");
  claudeHistoryGapState = reduce(claudeHistoryGapState, {
    type: "event", event: event({
      type: "state", sid: claudeHistoryGapSid, seq: 13, state: "idle",
    }),
  });
  assert.deepEqual(activeHistoryOwner(
    claudeHistoryGapState.runtimes[claudeHistoryGapSid]), [],
  "a later idle lifecycle still stops the History-recovered spark");

  const staleHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({ live_seq: 10 }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(staleHistoryOwner.liveOwner?.turnId, claudeHistoryGapMessage,
    "a History watermark behind live lifecycle cannot steal ownership");

  const oldGenerationOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({
      generation: "claude-history-old-generation",
    }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(oldGenerationOwner.liveOwner?.turnId, claudeHistoryGapMessage,
    "a History response cannot self-install an old generation as owner proof");

  const externalHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({ external: true }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(externalHistoryOwner.liveOwner?.turnId,
    claudeHistoryGapMessage,
    "external session activity cannot claim a browser-managed optimistic row");

  const otherHistoryHead = "claude-history-other-head";
  const nonHeadHistoryOwner = reduce(claudeHistoryGapBase, {
    type: "event", event: historyOnlyAcceptance({
      newest_id: otherHistoryHead,
      turns: [acceptedHistoryTurn, {
        id: otherHistoryHead, prompt: "newer external prompt", done: false,
        blocks: [], detailEventCount: 0, detailLoaded: false,
      }],
    }),
  }).runtimes[claudeHistoryGapSid];
  assert.notEqual(nonHeadHistoryOwner.liveOwner?.turnId,
    claudeHistoryGapMessage,
    "session-wide running state cannot reactivate an accepted non-head row");

  const slowProcessSid = "slow-process-clock";
  let slowProcessState = reduce({
    ...initialState,
    focusedSid: slowProcessSid,
    runtimes: {
      [slowProcessSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
    },
  }, {
    type: "query_sent", sid: slowProcessSid, prompt: "wait first",
    msg_id: "slow-process-turn", ts: 1_000,
  });
  for (const slowEvent of [
    event({
      type: "tool_use", sid: slowProcessSid, ts: 41,
      message_id: "slow-process-message", tool_use_id: "slow-process-tool",
      tool: "shell", input: { command: "true" },
    }),
    event({
      type: "tool_result", sid: slowProcessSid, ts: 42,
      tool_use_id: "slow-process-tool", content: "", is_error: false,
      status: "succeeded",
    }),
    event({
      type: "turn_end", sid: slowProcessSid, ts: 43,
      turn_id: "slow-process-turn",
      result: { subtype: "success", duration_ms: 42_000, is_error: false },
    }),
  ]) {
    slowProcessState = reduce(slowProcessState, {
      type: "event", event: slowEvent,
    });
  }
  const slowProcessTurn = slowProcessState.runtimes[slowProcessSid].turns[0];
  assert.equal(slowProcessTurn.processStartedTs, 41_000);
  assert.equal(slowProcessTurn.processDoneTs, 42_000,
    "the process clock freezes when the last visible tool settles");
  const slowProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: slowProcessSid, turns: [slowProcessTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(slowProcessMarkup, /已处理 1s/);
  assert.doesNotMatch(slowProcessMarkup, /已处理 42s/,
    "a late first process event never inherits the user-message wait");

  const diffSid = "diff-snapshot-clock";
  let diffState = reduce({
    ...initialState,
    focusedSid: diffSid,
    runtimes: {
      [diffSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
    },
  }, {
    type: "query_sent", sid: diffSid, prompt: "edit",
    msg_id: "diff-turn", ts: 1_000,
  });
  diffState = reduce(diffState, { type: "event", event: event({
    type: "turn_diff", sid: diffSid, ts: 41,
    item_id: "diff-snapshot", diff: "+done",
  }) });
  const diffTurn = diffState.runtimes[diffSid].turns[0];
  const diffBlock = diffTurn.blocks.find((block: { kind: string }) =>
    block.kind === "process");
  assert.equal(diffBlock?.done, true);
  assert.equal(diffTurn.processStartedTs, 41_000);
  assert.equal(diffTurn.processDoneTs, 41_000,
    "a complete diff snapshot does not run the process clock until TurnEnd");

  const stableSid = "stable-known-process";
  let stableState = {
    ...initialState,
    focusedSid: stableSid,
    runtimes: {
      [stableSid]: {
        ...createRuntime(),
        historyRevision: "stable-r1",
        turns: [{
          id: "stable-turn", prompt: "inspect", done: true,
          processDetailState: "present" as const,
          detailReasons: ["process" as const],
          detailEventCount: 9, detailLoaded: false, blocks: [],
        }],
      },
    },
  };
  stableState = reduce(stableState, { type: "event", event: event({
    type: "turn_detail", session_id: stableSid, turn_id: "stable-turn",
    revision: "stable-r1", events: [], before: null,
    has_more: false, has_newer: false, authoritative: true,
  }) });
  const stableAfterEmpty = stableState.runtimes[stableSid].turns[0];
  assert.equal(stableAfterEmpty.processDetailState, "present",
    "an empty raced detail response cannot erase known process evidence");
  assert.equal(stableAfterEmpty.detailEventCount, 9);
  assert.equal(stableAfterEmpty.detailLoaded, false);

  const unknownSid = "unknown-empty-detail";
  let unknownState = {
    ...initialState,
    focusedSid: unknownSid,
    runtimes: {
      [unknownSid]: {
        ...createRuntime(),
        historyRevision: "unknown-r1",
        turns: [{
          ...opaqueDirectSummary,
          id: "unknown-turn",
        }],
      },
    },
  };
  unknownState = reduce(unknownState, { type: "event", event: event({
    type: "turn_detail", session_id: unknownSid, turn_id: "unknown-turn",
    revision: "unknown-r1", events: [], before: null,
    has_more: false, has_newer: false, authoritative: true,
  }) });
  const unknownAfterEmpty = unknownState.runtimes[unknownSid].turns[0];
  assert.equal(unknownAfterEmpty.processDetailState, "none",
    "an exact empty detail response still refines an opaque direct answer");
  assert.equal(unknownAfterEmpty.detailLoaded, true);

  const unknownDetailMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "unknown-detail-session", turns: [opaqueDirectSummary],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(unknownDetailMarkup, /已处理/,
    "an opaque native summary must not claim that a process exists");
  assert.match(unknownDetailMarkup, /查看本轮详情/,
    "completed unknown detail keeps an honest on-demand entry");

  const incompleteKnownProcessMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline,
    {
      blocks: [], done: true, active: false, engine: "codex",
      deferredCount: 0, openOverride: true,
      detailError: "详细过程未完整返回，请重试",
      onLoadDetail: () => true,
    },
  ));
  assert.match(incompleteKnownProcessMarkup, /加载失败/);
  assert.match(incompleteKnownProcessMarkup, /详细过程未完整返回，请重试/);
  assert.doesNotMatch(incompleteKnownProcessMarkup, /正在加载过程/,
    "a contradictory complete detail becomes retryable instead of spinning");

  const pagedKnownProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "paged-known-process-session",
    turns: [{
      id: "paged-known-process-turn", prompt: "inspect", done: true,
      processDetailState: "present", detailReasons: ["process"],
      blocks: [], detailEventCount: 4, detailLoaded: true,
      detailHasMore: true, detailOldestCursor: "older-process-page",
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(pagedKnownProcessMarkup,
    /加载失败|详细过程未完整返回/,
    "an empty bounded window must not be mistaken for missing detail");
  assert.match(pagedKnownProcessMarkup, /更多过程/,
    "a bounded empty window retains its real pagination affordance");

  const retryingKnownProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "retrying-known-process-session",
      turns: [{
        id: "retrying-known-process-turn", prompt: "inspect", done: true,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 4, detailLoaded: true,
        detailLoading: true,
      }],
      engine: "codex", onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(retryingKnownProcessMarkup, /process-spin/);
  assert.doesNotMatch(retryingKnownProcessMarkup,
    /加载失败|详细过程未完整返回/,
    "a retry shows only its loading state until the response settles");

  const runningUnknownMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "running-unknown-session",
    turns: [{
      id: "running-unknown-turn", prompt: "wait", done: false,
      processDetailState: "unknown", detailReasons: [],
      blocks: [], detailEventCount: 0, detailLoaded: false,
    }],
    engine: "codex", activeTurnId: "running-unknown-turn",
    onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(runningUnknownMarkup, /正在处理|已处理|查看本轮详情/);
  assert.match(runningUnknownMarkup, /思考中/,
    "running unknown detail uses only the live tail indicator");

  const runningUntimedProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "running-untimed-process-session",
      turns: [{
        id: "running-untimed-process-turn", prompt: "work", done: false,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 5, detailLoaded: false,
      }],
      engine: "codex", activeTurnId: "running-untimed-process-turn",
      onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(runningUntimedProcessMarkup, />正在处理</);
  assert.doesNotMatch(runningUntimedProcessMarkup, /正在处理 0s/,
    "a process without a trustworthy first event never invents a timer");

  const zeroTimedProcessMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: "zero-timed-process-session",
      turns: [{
        id: "zero-timed-process-turn", prompt: "work", done: true,
        processDetailState: "present", detailReasons: ["process"],
        blocks: [], detailEventCount: 1, detailLoaded: false,
        processStartedTs: 42_000, processDoneTs: 42_000,
      }],
      engine: "codex", onEdit: () => {}, onGetDiff: () => {},
      onLoadDetail: () => {},
    },
  ));
  assert.match(zeroTimedProcessMarkup, />已处理</);
  assert.doesNotMatch(zeroTimedProcessMarkup, /已处理 0s/,
    "equal cached process timestamps keep the label but omit a fake duration");

  const directAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "direct-answer-session", turns: [exactDirectDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(directAnswerMarkup,
    /已处理|查看本轮详情|查看完整内容/,
    "an exact direct answer has no process or generic-detail affordance");

  const truncatedAnswerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "truncated-answer-session",
    turns: [{
      id: "truncated-answer-turn", prompt: "long", done: true,
      processDetailState: "none", detailReasons: ["answer_truncated"],
      blocks: [{
        kind: "text", message_id: "truncated-answer-final",
        channel: "final", text: "prefix…", done: true,
      }],
      detailEventCount: 1, detailLoaded: false,
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(truncatedAnswerMarkup, /已处理/);
  assert.match(truncatedAnswerMarkup, /查看完整内容/,
    "truncated prose uses a content affordance instead of the process shell");
} finally {
  await harness.close();
}
