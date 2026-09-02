import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  commandsFor,
  isKnownCodeOnlySlash,
  matchCommands,
} from "../src/data.ts";

const slashes = (engine: "claude" | "codex") => commandsFor(engine, "work")
  .filter((command) => "slash" in command)
  .map((command) => command.slash);
const claudeWorkSlashes = slashes("claude");
const codexWorkSlashes = slashes("codex");

assert.deepEqual(
  codexWorkSlashes.filter((slash) => slash !== "fast"),
  claudeWorkSlashes,
  "Codex Work must add only Fast to the shared Work command surface",
);
assert.equal(codexWorkSlashes.filter((slash) => slash === "fast").length, 1);
assert.equal(claudeWorkSlashes.includes("fast"), false);
assert.equal(isKnownCodeOnlySlash("fast", "codex"), false,
  "Work must execute /fast locally instead of rejecting it as Code-only");
assert.deepEqual(
  matchCommands("fas", "codex", "work").map((command) => command.slash),
  ["fast"],
);
assert.deepEqual(matchCommands("fas", "claude", "work"), []);

const composerSource = readFileSync(resolve(
  process.cwd(), "src/components/Composer.tsx"), "utf8");
assert.match(composerSource,
  /p\.engine === "codex"[\s\S]{0,220}className="work-fast-setting"[\s\S]{0,320}p\.onSetServiceTier\?\.\("toggle"\)/,
  "existing Codex Work sessions must expose their authoritative Fast toggle");

const newChatSource = readFileSync(resolve(
  process.cwd(), "src/components/NewChatView.tsx"), "utf8");
assert.match(newChatSource,
  /engine === "codex" && space === "work"[\s\S]{0,260}aria-label="新工作 Fast 服务档位"/,
  "only a new Codex Work form should expose the Fast selection");
assert.match(newChatSource, /engine === "codex" \? serviceTier : undefined/,
  "the atomic first turn must carry the selected service tier");
