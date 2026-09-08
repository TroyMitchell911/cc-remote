import assert from "node:assert/strict";
import { MODELS, matchModelId } from "../src/data.ts";

const expected = new Map([
  ["claude-fable-5-1", "Fable 5.1"],
  ["claude-mythos-5-1", "Mythos 5.1"],
]);

for (const [id, name] of expected) {
  const model = MODELS.find((candidate) => candidate.id === id);
  assert.ok(model, `${id} must be available in the curated Claude catalog`);
  assert.equal(model.name, name);
  assert.match(model.ds, /1M 上下文/);
  assert.equal(matchModelId(`${id}[1m]`, "claude"), id);
}

assert.equal(MODELS.some((model) => (
  model.id === "claude-fable-5" || model.id === "claude-mythos-5"
)), false, "retired curated cards must not remain selectable");
assert.equal(matchModelId("claude-mythos-5", "claude"),
  "claude-mythos-5",
  "historical sessions must not be relabelled as Mythos 5.1");
