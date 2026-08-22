import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createServer } from "vite";

import type { ServerEvent } from "../src/protocol.ts";
import { selectSurfaceSession } from "../src/session-order.ts";
import { scopedFocusForSessionList } from "../src/session-list.ts";

assert.equal(scopedFocusForSessionList(
  "claude-before-switch",
  [{ session_id: "claude-before-switch", engine: "claude", space: "code" }],
  "codex-user-click",
  "codex",
  "code",
), "codex-user-click",
"a late Codex list validates the synchronous Codex click, not stale Claude state");
assert.equal(scopedFocusForSessionList(
  "codex-committed",
  [{ session_id: "codex-committed", engine: "codex", space: "code" }],
  "older-bookmark",
  "codex",
  "code",
), "codex-committed",
"committed focus on the listed surface remains authoritative over its bookmark");

const recentProject = {
  session_id: "project-new",
  cwd: "/home/nancy/project",
  last_modified: "300",
};
const projectOld = {
  session_id: "project-old",
  cwd: "/home/nancy/project",
  last_modified: "200",
};
const archivedNewest = {
  session_id: "archived-newest",
  last_modified: "400",
  tag: "archived" as const,
};
assert.equal(selectSurfaceSession(
  [recentProject, projectOld, archivedNewest], "project-old")?.session_id,
"project-old",
"a surface toggle must restore the exact last-viewed session immediately");
assert.equal(selectSurfaceSession(
  [archivedNewest, projectOld, recentProject])?.session_id,
"project-new",
"without a bookmark, a surface toggle must paint its newest visible session");

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
assert.match(appSource,
  /const immediate = preserveAuthority[\s\S]{0,180}selectSurfaceSession\(cachedSessions, remembered\)/,
  "ordinary surface switches must select from the scoped cached list synchronously");
assert.match(appSource,
  /if \(immediate\) \{[\s\S]{0,500}dispatch\(\{ type: "focus_session", sid: immediate\.session_id \}\)[\s\S]{0,500}sendSwitchSession\(immediate\.session_id, nextEngine, nextSpace\)/,
  "cached surface focus must paint and resume immediately while authority refreshes");
assert.match(appSource,
  /dispatch\(\{ type: "exit_new_chat" \}\);\s*setRestoringSurfaceScope\(focusScopeKey\)/,
  "a cold surface switch must hide the sendable default draft until its list arrives");
assert.match(appSource,
  /restoringSurfaceScope === activeScopeKey \? \([\s\S]{0,220}正在恢复会话/,
  "only the exact active machine/engine/space restore scope may show the loading gate");
assert.match(appSource,
  /!state\.newChat \|\| newChatCodexProfileMissing\s*\|\| restoringSurfaceScope === activeScopeKey/,
  "the first-message path must fail closed even if a stale composer invokes it");
assert.match(appSource,
  /if \(!current\.newChat\) \{[\s\S]{0,500}cwd: inheritedCwd \|\| "~"[\s\S]{0,500}scope === focusScopeKey \? null : scope/,
  "an authoritative empty list alone may create the scoped default New Chat draft");
assert.match(appSource,
  /const focusListedSession = useCallback\([\s\S]{0,800}didInitFocusRef\.current = true;[\s\S]{0,120}preferredSurfaceFocusRef\.current = null;[\s\S]{0,120}lastFocusBySurfaceRef\.current\[focusScopeKey\] = id;/,
  "an explicit session click must synchronously revoke pending surface restoration");
assert.match(appSource,
  /const scopedCurrentSid = scopedFocusForSessionList\([\s\S]{0,500}normalizedListedSessions\?\.some/,
  "a late list must validate focus only inside its own engine and space");

const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { initialState, reduce } = await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const explicitDraftAfterMissingFocus = reduce({
    ...initialState,
    sessions: [{
      session_id: "deleted-behind-draft", engine: "claude", space: "code",
    }],
    focusedSid: "deleted-behind-draft",
    newChat: {
      cwd: "/Volumes/MuggleSSD/workspace/robot-agent",
      cwdSource: "explicit" as const,
      model: null,
      effort: null,
      codexProfileId: null,
    },
  }, { type: "event", event: {
    v: 37,
    ts: 10,
    type: "session_list",
    engine: "claude",
    space: "code",
    sessions: [{
      session_id: "replacement", engine: "claude", space: "code",
    }],
  } as ServerEvent });
  assert.equal(explicitDraftAfterMissingFocus.focusedSid, null);
  assert.equal(explicitDraftAfterMissingFocus.newChat?.cwd,
    "/Volumes/MuggleSSD/workspace/robot-agent");
  assert.equal(explicitDraftAfterMissingFocus.newChat?.cwdSource, "explicit",
    "a missing former focus must not replace the user's explicit New Chat cwd");
} finally {
  await reducerHarness.close();
}
