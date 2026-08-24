import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import {
  normalizeAutoCompactSelection,
  parseAutoCompactArgument,
  validAutoCompactThreshold,
} from "../src/auto-compact.ts";
import { clientSlashesFor } from "../src/data.ts";
import type { ServerEvent } from "../src/protocol.ts";
import { RelayWs } from "../src/ws.ts";


assert.equal(clientSlashesFor("claude").has("autocompact"), true);
assert.equal(clientSlashesFor("codex").has("autocompact"), true,
  "Codex must intercept the shared Work command and reject it locally");
assert.deepEqual(parseAutoCompactArgument("inherit"), {
  ok: true,
  selection: { mode: "inherit", thresholdTokens: null },
});
assert.deepEqual(parseAutoCompactArgument("auto"), {
  ok: true,
  selection: { mode: "auto", thresholdTokens: null },
});
assert.deepEqual(parseAutoCompactArgument("250k"), {
  ok: true,
  selection: { mode: "custom", thresholdTokens: 250_000 },
});
assert.deepEqual(parseAutoCompactArgument("0.5m"), {
  ok: true,
  selection: { mode: "custom", thresholdTokens: 500_000 },
});
assert.equal(parseAutoCompactArgument("99999").ok, false);
assert.equal(parseAutoCompactArgument("100.0005k").ok, false);
assert.equal(validAutoCompactThreshold(100_000), true);
assert.equal(validAutoCompactThreshold(1_000_001), false);
assert.deepEqual(normalizeAutoCompactSelection("custom", null), {
  mode: "inherit", thresholdTokens: null,
});

const composerSource = readFileSync(resolve(
  process.cwd(), "src/components/Composer.tsx"), "utf8");
const newChatSource = readFileSync(resolve(
  process.cwd(), "src/components/NewChatView.tsx"), "utf8");
const btwSource = readFileSync(resolve(
  process.cwd(), "src/components/BtwPanel.tsx"), "utf8");
assert.doesNotMatch(composerSource, /hint-auto-compact/,
  "autocompact must not occupy a persistent main-composer control");
assert.doesNotMatch(composerSource, /<span>自动压缩<\/span><b>/,
  "Work settings must not expose a persistent autocompact row");
assert.match(composerSource,
  /if \(slash === "autocompact"\) \{[\s\S]{0,120}setInput\("\/autocompact "\)/,
  "choosing the command suggestion must wait for an explicit send");
assert.doesNotMatch(newChatSource, /auto-compact-chip/,
  "new-chat autocompact must remain command-only");
assert.doesNotMatch(btwSource, /压缩 ·/,
  "BTW autocompact must remain command-only");
for (const source of [newChatSource, btwSource]) {
  assert.match(source, /command\?\.slash === "autocompact"/,
    "secondary composers must intercept autocompact before model submission");
  assert.match(source,
    /if \(!command\.args\) \{[\s\S]{0,160}setAutoCompactOpen\(true\)/,
    "a bare autocompact command must open its hidden editor");
  assert.match(source, /parseAutoCompactArgument\(command\.args\)/,
    "a parameterized autocompact command must apply directly");
}


class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {
    this.readyState = 3;
  }
}

Object.assign(globalThis, {
  window: {
    location: { protocol: "http:", host: "relay.test" },
  },
  WebSocket: FakeWebSocket,
});

const relay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
relay.start();
const socket = FakeWebSocket.instances.at(-1);
assert.ok(socket);
socket.onopen?.();

relay.setFocusedSid("claude-autocompact", "claude", "code");
assert.equal(relay.sendSetAutoCompact("custom", 250_000), true);
const autoCompactFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(autoCompactFrame.type, "set_auto_compact");
assert.equal(autoCompactFrame.sid, "claude-autocompact");
assert.equal(autoCompactFrame.mode, "custom");
assert.equal(autoCompactFrame.threshold_tokens, 250_000);
assert.equal(typeof autoCompactFrame.cmd_id, "string");
assert.equal(typeof autoCompactFrame.client_id, "string");

assert.equal(relay.sendSetAutoCompactTo("btw-pinned", "auto"), true);
const btwAutoCompactFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwAutoCompactFrame.type, "set_auto_compact");
assert.equal(btwAutoCompactFrame.sid, "btw-pinned");
assert.equal(btwAutoCompactFrame.mode, "auto");
assert.equal("threshold_tokens" in btwAutoCompactFrame, false);

const beforeInvalidAutoCompact = socket.sent.length;
assert.equal(relay.sendSetAutoCompact("custom", null), false);
assert.equal(relay.sendSetAutoCompactTo(
  "btw-pinned", "custom", 99_999), false);
assert.equal(socket.sent.length, beforeInvalidAutoCompact,
  "invalid custom thresholds must never enter the reliable outbox");

assert.equal(relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "custom compact", msg_id: "autocompact-create-message" },
  undefined, undefined, undefined, undefined, undefined,
  "code", null, null,
  { mode: "custom", thresholdTokens: 250_000 },
), true);
const autoCompactSessionFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(autoCompactSessionFrame.type, "new_session");
assert.equal(autoCompactSessionFrame.auto_compact_mode, "custom");
assert.equal(
  autoCompactSessionFrame.auto_compact_threshold_tokens, 250_000);
assert.equal(autoCompactSessionFrame.prompt, "custom compact");

const beforeInvalidAutoCompactSession = socket.sent.length;
assert.equal(relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "invalid compact", msg_id: "invalid-autocompact-create" },
  undefined, undefined, undefined, undefined, undefined,
  "code", null, null,
  { mode: "custom", thresholdTokens: null },
), false);
assert.equal(socket.sent.length, beforeInvalidAutoCompactSession,
  "an invalid create request must not reserve focus ownership or send a frame");
relay.stop();


const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { createRuntime, initialState, reduce } =
    await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 38,
    ts: 10,
    ...body,
  } as ServerEvent);

  assert.equal(createRuntime().autoCompact, null,
    "a session must not claim a mode before the wrapper reports it");
  const sid = "claude-autocompact-state";
  const state = reduce({
    ...initialState,
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  }, {
    type: "event",
    event: event({
      type: "auto_compact",
      sid,
      mode: "custom",
      threshold_tokens: 250_000,
      applied_mode: "inherit",
      pending: true,
      mutable: true,
    }),
  });
  assert.equal(state.runtimes[sid].autoCompact.mode, "custom");
  assert.equal(state.runtimes[sid].autoCompact.threshold_tokens, 250_000);
  assert.equal(state.runtimes[sid].autoCompact.applied_mode, "inherit");
  assert.equal(state.runtimes[sid].autoCompact.pending, true);

  const newChat = reduce(reduce(initialState, {
    type: "enter_new_chat",
    cwd: "/repo",
  }), {
    type: "set_new_chat_auto_compact",
    mode: "custom",
    thresholdTokens: 500_000,
  });
  assert.equal(newChat.newChat?.autoCompactMode, "custom");
  assert.equal(newChat.newChat?.autoCompactThresholdTokens, 500_000);
} finally {
  await reducerHarness.close();
}

console.log("Claude autocompact tests passed");
