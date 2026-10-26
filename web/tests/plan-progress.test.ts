import assert from "node:assert/strict";
import { createServer } from "vite";

import type { ProcessBlock, Turn } from "../src/domain/conversation.js";
import {
  latestPlanProgress,
  claudePlanProgress, claudePlanDismissLabel, planDocumentTitle,
  planProgressPresentation,
  SessionPlanProgressCache,
  settleClaudePlanMode,
} from "../src/plan-progress.js";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.js";

const activePlanBlock: ProcessBlock = {
  kind: "process",
  item_id: "active-plan",
  processKind: "plan",
  phase: "update",
  status: "running",
  title: "计划",
  plan: [
    { step: "检查实现", status: "completed" },
    { step: "完成验证", status: "inProgress" },
  ],
  done: false,
};

const staleTerminalPlanOwner: Turn = {
  id: "stale-terminal-plan-turn",
  prompt: "执行但忘记更新计划",
  done: true,
  blocks: [{
    ...activePlanBlock,
    status: "succeeded",
    done: true,
  }],
};
const staleTerminalProgress = latestPlanProgress([staleTerminalPlanOwner]);
assert.ok(staleTerminalProgress);
assert.deepEqual(
  {
    stale: planProgressPresentation(staleTerminalProgress.block).stale,
    currentStep: planProgressPresentation(
      staleTerminalProgress.block).currentStep,
    stateLabel: planProgressPresentation(
      staleTerminalProgress.block).stateLabel,
  },
  {
    stale: true,
    currentStep: null,
    stateLabel: "本轮已结束，计划未更新",
  },
  "a successful terminal must not present the last stale step as active",
);

assert.equal(latestPlanProgress([staleTerminalPlanOwner, {
  id: "after-stale-terminal-plan",
  prompt: "开始新任务",
  done: false,
  blocks: [],
}]), null,
"the next user message retires a terminal plan whose steps were stale");

const recoveredTerminalPlan = latestPlanProgress([{
  ...staleTerminalPlanOwner,
  blocks: [{ ...activePlanBlock, done: true, status: "succeeded" }],
}]);
assert.equal(recoveredTerminalPlan?.block.done, true,
  "a durable snapshot retains its exact terminal without changing steps");
assert.equal(
  planProgressPresentation(recoveredTerminalPlan!.block).stale,
  true,
);

const steeredPlanOwner: Turn = {
  id: "steered-plan-turn",
  prompt: "执行任务",
  done: true,
  blocks: [{ ...activePlanBlock }],
};
const steeredPlan = latestPlanProgress([steeredPlanOwner, {
  id: "steer-follow-up",
  prompt: "补充要求",
  done: false,
  blocks: [],
}]);
assert.equal(steeredPlan?.turnId, "steered-plan-turn",
  "a neutral steer boundary keeps its open Plan across the clarification");
assert.equal(planProgressPresentation(steeredPlan!.block).currentStep,
  "完成验证");

const event = (body: Record<string, unknown>): ServerEvent => ({
  v: PROTOCOL_VERSION,
  ts: 1,
  ...body,
}) as ServerEvent;
const reducerSid = "steered-plan-reducer";
const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const claudeSid = "echo@claude-planning";
  const otherSid = "iris@other-session";
  const modePlan: ProcessBlock = {
    kind: "process", item_id: "claude-plan", processKind: "plan",
    title: "计划模式", summary: "正在制定计划", phase: "start",
    status: "running", done: false,
  };
  const owner: Turn = {
    id: "claude-plan-turn", prompt: "出计划", done: false,
    blocks: [modePlan],
  };
  let claudeState = {
    ...initialState, focusedSid: otherSid,
    runtimes: {
      [claudeSid]: { ...createRuntime(), turns: [owner] },
      [otherSid]: { ...createRuntime(), perm: "acceptEdits" },
    },
  };
  const cache = new SessionPlanProgressCache();
  const resolve = (mode: string, fromHistory = false) => cache.resolve({
    engine: "claude", sid: claudeSid, permissionMode: mode,
    runtime: fromHistory ? null : latestPlanProgress([owner]),
    history: fromHistory ? latestPlanProgress([owner]) : null,
    runtimeTurns: fromHistory ? [] : [owner],
    historyTurns: fromHistory ? [owner] : [],
    recovering: false, runtimeLoading: false,
  });
  assert.equal(resolve("")?.block.done, false,
    "unknown permission must not close a live plan while reconnecting");
  for (const mode of ["plan", "bypassPermissions", "plan", "default", "acceptEdits", "auto"]) {
    claudeState = reduce(claudeState, { type: "event", event: event({
      type: "perm", sid: claudeSid, mode,
    }) });
    assert.equal(claudeState.runtimes[claudeSid].perm, mode);
    assert.equal(claudeState.runtimes[otherSid].perm, "acceptEdits");
    assert.equal(claudeState.focusedSid, otherSid);
    for (const fromHistory of [false, true]) {
      const progress = resolve(claudeState.runtimes[claudeSid].perm, fromHistory)!;
      assert.equal(progress.block.done, mode !== "plan");
      if (mode !== "plan") {
        assert.equal(progress.block.summary, "已退出计划模式");
        assert.equal(planProgressPresentation(progress.block).complete, false,
          "leaving plan mode is not proof that a plan was completed");
        assert.notEqual(planProgressPresentation(progress.block).progressLabel, "执行中");
      }
    }
  }
  assert.equal(modePlan.done, false, "display correction preserves original history");
  assert.equal(cache.resolve({
    engine: "claude", sid: claudeSid, permissionMode: "bypassPermissions",
    runtime: latestPlanProgress([owner]), history: null,
    runtimeTurns: [owner, { id: "next", prompt: "继续", blocks: [], done: false }],
    historyTurns: [], recovering: false, runtimeLoading: false,
  }), null, "the next user turn retires an exited plan-mode marker");
  assert.equal(settleClaudePlanMode(
    { turnId: owner.id, block: activePlanBlock, detailLoading: false, needsDetail: false },
    "codex", "never",
  )?.block, activePlanBlock, "Codex checklists are independent from permission mode");
  const { PlanProgressContent } = await reducerHarness.ssrLoadModule(
    "/src/components/PlanProgressPopover.tsx");
  const React = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const closedPlan = resolve("bypassPermissions")!;
  const html = renderToStaticMarkup(React.createElement(PlanProgressContent, {
    block: closedPlan.block,
  }));
  assert.ok(html.includes("已退出计划模式"));
  assert.ok(!html.includes("等待计划步骤"));

  const canonicalPlan = {
    id: "native-plan", revision: 3, turn_id: "old-owner",
    document: "# Migration plan\n\n## Verify\n\n- Run tests\n- Review changes",
    phase: "approved" as const, steps: [], dismissed: false,
  };
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid, plan: canonicalPlan,
  }) });
  const canonical = claudePlanProgress(claudeState.runtimes[claudeSid].claudePlan)!;
  assert.equal(canonical.block.explanation, canonicalPlan.document);
  assert.equal(canonical.needsDetail, false);
  assert.equal(claudeState.focusedSid, otherSid);
  assert.equal(planDocumentTitle(canonicalPlan.document), "Migration plan");
  assert.equal(planProgressPresentation(canonical.block).stateLabel, "已批准");
  assert.equal(planProgressPresentation(canonical.block).complete, false);
  assert.equal(claudePlanDismissLabel(canonicalPlan), "确认已完成");
  assert.equal(claudePlanDismissLabel({ ...canonicalPlan, phase: "pending" }), null);
  assert.equal(claudePlanDismissLabel({ ...canonicalPlan,
    steps: [{ step: "Test", status: "pending" }] }), null);
  assert.equal(claudePlanDismissLabel({ ...canonicalPlan,
    steps: [{ step: "Test", status: "completed" }] }), "已完成 · 收起");
  assert.equal(claudePlanDismissLabel({ ...canonicalPlan, document: "",
    steps: [{ step: "Test", status: "completed" }] }), "已完成 · 收起");
  const pendingWithOldSteps = claudePlanProgress({ ...canonicalPlan, phase: "pending",
    steps: [{ step: "Previous draft", status: "completed" }] })!;
  assert.equal(planProgressPresentation(pendingWithOldSteps.block).progressLabel, "待确认");
  assert.equal(planProgressPresentation(pendingWithOldSteps.block).complete, false);
  const planHtml = renderToStaticMarkup(React.createElement(PlanProgressContent, {
    block: canonical.block, onDismiss() {},
  }));
  assert.ok(planHtml.includes("<h1>Migration plan</h1>"));
  assert.ok(planHtml.includes("<h2>Verify</h2>"));
  assert.ok(planHtml.includes("<li>Run tests</li>"));
  assert.ok(planHtml.includes("确认已完成"));
  assert.ok(!planHtml.includes("等待计划步骤"));
  const { QuestionSheet } = await reducerHarness.ssrLoadModule("/src/components/QuestionSheet.tsx");
  const approvalHtml = renderToStaticMarkup(React.createElement(QuestionSheet, {
    engine: "claude", question: "请确认计划及批准后的权限", header: "批准计划",
    planDocument: canonicalPlan.document, options: [
      { label: "逐次确认" }, { label: "自动接受编辑" }, { label: "继续修改" },
    ], onAnswer() {},
  }));
  assert.ok(approvalHtml.includes("<h1>Migration plan</h1>"));
  assert.ok(approvalHtml.includes("批准后使用的权限"));
  assert.ok(approvalHtml.includes('type="radio"'));
  assert.ok(approvalHtml.includes('checked="" value="逐次确认"'));
  assert.ok(!approvalHtml.includes("<select"));
  assert.ok(approvalHtml.includes("plan-approval-footer"));
  assert.ok(approvalHtml.includes(">继续修改</button>"));
  assert.ok(approvalHtml.includes(">批准计划</button>"));
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "user_msg", sid: claudeSid, msg_id: "later", prompt: "Continue",
  }) });
  assert.ok(claudePlanProgress(claudeState.runtimes[claudeSid].claudePlan),
    "new user messages cannot retire a retained document");
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid,
    plan: { ...canonicalPlan, revision: 4, dismissed: true },
  }) });
  // A delayed read from before the receipt cannot reopen the monitor.
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid, plan: canonicalPlan,
  }) });
  assert.equal(claudePlanProgress(claudeState.runtimes[claudeSid].claudePlan), null);

  const restoredPlan = { ...canonicalPlan, revision: 5,
    steps: [{ step: "Verify", status: "pending" as const }] };
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid, plan: restoredPlan,
  }) });
  assert.ok(claudePlanProgress(claudeState.runtimes[claudeSid].claudePlan),
    "rollback reopens the retained plan using a newer revision");
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid,
    plan: { ...canonicalPlan, revision: 4, dismissed: true },
  }) });
  assert.deepEqual(claudeState.runtimes[claudeSid].claudePlan, restoredPlan,
    "a pre-rollback dismissal cannot hide the restored checklist");
  const continuedPlan = { ...restoredPlan, revision: 6,
    steps: [{ step: "Verify", status: "inProgress" as const }] };
  claudeState = reduce(claudeState, { type: "event", event: event({
    type: "claude_plan_state", sid: claudeSid, plan: continuedPlan,
  }) });
  assert.deepEqual(claudeState.runtimes[claudeSid].claudePlan, continuedPlan);

  let reducerState = reduce({
    ...initialState,
    focusedSid: reducerSid,
    runtimes: { [reducerSid]: createRuntime() },
  }, {
    type: "query_sent",
    sid: reducerSid,
    prompt: "执行任务",
    msg_id: "native-steered-plan",
    ts: 1_000,
  });
  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_plan",
    sid: reducerSid,
    item_id: "plan:native-steered-plan",
    turn_id: "native-steered-plan",
    explanation: null,
    plan: activePlanBlock.plan,
  }) });
  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_end",
    sid: reducerSid,
    turn_id: "native-steered-plan",
    result: { subtype: "steered", duration_ms: 0, is_error: false },
  }) });
  const reducerPlan = reducerState.runtimes[reducerSid].turns[0].blocks.find(
    (block: ProcessBlock) =>
      block.kind === "process" && block.processKind === "plan",
  );
  assert.equal(reducerPlan?.done, false,
    "a live neutral steer boundary must not settle the Plan block");

  const detailSid = "expanded-neutral-steer-plan";
  let detailState = {
    ...initialState,
    focusedSid: detailSid,
    runtimes: {
      [detailSid]: {
        ...createRuntime(),
        historyRevision: "expanded-plan-revision",
        historyGeneration: "expanded-plan-generation",
        turns: [{
          id: "expanded-plan-turn", prompt: "execute", done: true,
          detailEventCount: 2, detailLoaded: true,
          blocks: [],
          detailProjection: {
            segments: [], capped: false, hasMore: false,
            oldestCursor: null, hasNewer: false, newerCursor: null,
            blocks: [{ ...activePlanBlock, item_id: "expanded-plan" }, {
              kind: "process", item_id: "expanded-command",
              processKind: "command", phase: "start", status: "running",
              title: "command", done: false,
            }],
          },
        }],
      },
    },
  };
  detailState = reduce(detailState, { type: "event", event: event({
    type: "history", sid: detailSid, session_id: detailSid,
    revision: "expanded-plan-revision",
    generation: "expanded-plan-generation", build_seq: 1,
    detail: "summary", in_progress: true, has_more: false, events: [],
    turns: [{
      id: "expanded-plan-turn", prompt: "execute", done: true,
      detailEventCount: 2, detailLoaded: false, blocks: [],
    }],
  }) });
  const reconciledDetail = detailState.runtimes[detailSid].turns[0]
    .detailProjection?.blocks ?? [];
  const reconciledPlan = reconciledDetail.find((block: ProcessBlock) =>
    block.kind === "process" && block.processKind === "plan");
  const reconciledCommand = reconciledDetail.find((block: ProcessBlock) =>
    block.kind === "process" && block.processKind === "command");
  assert.equal(reconciledPlan?.done, false,
    "summary refresh preserves an expanded neutral-steer Plan");
  assert.equal(reconciledCommand?.done, true,
    "summary refresh still settles ordinary expanded-detail children");

  let idleDetailState = {
    ...initialState,
    focusedSid: detailSid,
    runtimes: {
      [detailSid]: {
        ...createRuntime(),
        historyRevision: "expanded-plan-revision",
        historyGeneration: "expanded-plan-generation",
        turns: [{
          id: "expanded-plan-turn", prompt: "execute", done: true,
          detailEventCount: 1, detailLoaded: true,
          blocks: [],
          detailProjection: {
            segments: [], capped: false, hasMore: false,
            oldestCursor: null, hasNewer: false, newerCursor: null,
            blocks: [{ ...activePlanBlock, item_id: "idle-expanded-plan" }],
          },
        }],
      },
    },
  };
  idleDetailState = reduce(idleDetailState, {
    type: "event", event: event({
      type: "history", sid: detailSid, session_id: detailSid,
      revision: "expanded-plan-revision",
      generation: "expanded-plan-generation", build_seq: 1,
      detail: "summary", in_progress: false, has_more: false, events: [],
      turns: [{
        id: "expanded-plan-turn", prompt: "execute", done: true,
        detailEventCount: 1, detailLoaded: false, blocks: [],
      }],
    }),
  });
  const idleTurn = idleDetailState.runtimes[detailSid].turns[0];
  const idlePlan = idleTurn.detailProjection?.blocks.find(
    (block: ProcessBlock) => block.kind === "process"
      && block.processKind === "plan",
  );
  assert.equal(idlePlan?.done, true,
    "an authoritative idle History settles an old cached Plan");
  assert.equal(
    latestPlanProgress([idleTurn, {
      id: "after-idle-plan", prompt: "next task", done: true, blocks: [],
    }]),
    null,
    "a settled stale Plan retires at the next user boundary",
  );

  reducerState = reduce(reducerState, { type: "event", event: event({
    type: "turn_end",
    sid: reducerSid,
    turn_id: "native-steered-plan",
    result: { subtype: "success", duration_ms: 1000, is_error: false },
  }) });
  const terminalReducerPlan = reducerState.runtimes[reducerSid].turns[0]
    .blocks.find((block: ProcessBlock) =>
      block.kind === "process" && block.processKind === "plan");
  assert.equal(terminalReducerPlan?.done, true,
    "the later exact terminal settles the same Plan block");
} finally {
  await reducerHarness.close();
}

const terminalPlanWithoutSteps: Turn = {
  id: "terminal-empty-plan",
  prompt: "执行无结构计划",
  done: true,
  blocks: [{
    ...activePlanBlock,
    item_id: "terminal-empty-plan-block",
    status: "succeeded",
    plan: [],
    done: true,
  }],
};
assert.equal(latestPlanProgress([terminalPlanWithoutSteps, {
  id: "after-terminal-empty-plan",
  prompt: "下一项任务",
  done: false,
  blocks: [],
}]), null, "a terminal plan without steps retires at the next user turn");

console.log("plan progress tests passed");
