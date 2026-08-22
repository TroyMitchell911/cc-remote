import { strict as assert } from "node:assert";
import {
  effortIsSelectable,
  effortNameForDisplay,
} from "../src/data.js";
import { accountStatsNote, shouldOpenCodexStatus } from "../src/status-capabilities.js";

assert.equal(
  effortNameForDisplay("model-default"),
  "模型默认",
  "an unresolved official model default must not remain in loading state",
);
assert.equal(
  effortNameForDisplay(""),
  null,
  "an unseeded effort must retain the loading label instead of painting blank",
);
assert.equal(
  effortNameForDisplay("xhigh"),
  "xhigh",
  "ordinary effort labels retain the raw CLI/config id contract",
);
assert.equal(effortIsSelectable("model-default"), false,
  "the model-default display sentinel is never a selectable effort override");
assert.equal(effortIsSelectable("high"), true,
  "ordinary effort ids remain selectable");

assert.equal(shouldOpenCodexStatus(null, null, "claude"), false);
assert.equal(shouldOpenCodexStatus(null, null, "codex"), false);
assert.equal(shouldOpenCodexStatus("s1", "s1", "claude"), false);
assert.equal(shouldOpenCodexStatus("s1", "s2", "codex"), false);
assert.equal(shouldOpenCodexStatus("s1", "s1", "codex"), true);

assert.equal(accountStatsNote({
  auth_type: "apiKey", requires_openai_auth: true,
}), "当前 API Key 模式不提供 ChatGPT 订阅限额和账户用量。");

assert.equal(accountStatsNote({
  auth_type: "amazonBedrock", requires_openai_auth: false,
}), "当前 Amazon Bedrock 模式不提供 ChatGPT 订阅限额和账户用量。");

assert.equal(accountStatsNote({
  auth_type: "chatgpt", plan_type: "pro", requires_openai_auth: false,
}), null);
assert.equal(accountStatsNote(null), null);

console.log("status capability tests passed");
