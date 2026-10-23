import { useEffect, useReducer, useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import { useChatReadingMemory } from "../../src/chat-reading-memory";
import { createRuntime, initialState, reduce, type Turn } from "../../src/reducer";
import { displayHistoryProjection } from "../../src/history-recovery";

function turn(id: string): Turn {
  return { id, done: true, prompt: id, blocks: [{ kind: "text", done: true, message_id: `${id}-answer`,
    text: Array.from({ length: 20 }, (_, i) => `${id} paragraph ${i}. Reading a long answer while another session continues working.`).join("\n\n"),
  }] };
}

export function ReadingNavigationFixture() {
  const [engine, setEngine] = useState<"codex" | "claude">("codex");
  const [machine, setMachine] = useState("device-a");
  const [state, dispatch] = useReducer(reduce, null, () => ({ ...initialState,
    focusedSid: "codex-session",
    runtimes: Object.fromEntries(["codex", "claude"].map(e => [`${e}-session`, {
      ...createRuntime(), historyRevision: "revision-1", historyGeneration: "generation-1",
      historyHeadKnown: true, hasMore: true, oldestId: `${e}-1`,
      turns: [turn(`${e}-1`), turn(`${e}-2`), turn(`${e}-3`)],
    }])),
  }));
  const sid = state.focusedSid!;
  const runtime = state.runtimes[sid];
  const scope = `${machine}:code:${engine}`;
  const memory = useChatReadingMemory(scope, sid, runtime, state.historyBrowse, dispatch);
  const view = displayHistoryProjection(state.historyRecovery, sid, runtime, state.historyBrowse);
  const navigate = (e: "codex" | "claude") => {
    dispatch({ type: "restore_session_list", sessions: [] });
    dispatch({ type: "focus_session", sid: `${e}-session` });
    setEngine(e);
  };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.key === "1" || event.key === "2") {
        event.preventDefault(); navigate(event.key === "1" ? "claude" : "codex");
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  return <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <div>
      <button onClick={() => navigate("claude")}>Claude</button>
      <button onClick={() => navigate("codex")}>Codex</button>
      <button onClick={() => setMachine(m => m === "device-a" ? "device-b" : "device-a")}>Switch device</button>
      <button onClick={() => dispatch({ type: "set_turns", sid: "codex-session",
        turns: [...state.runtimes["codex-session"].turns, turn("background-output")] })}>Background output</button>
      <button onClick={() => dispatch({ type: "begin_history_browse", sid,
        revision: runtime.historyRevision!, generation: runtime.historyGeneration,
        scopeKey: scope, viewId: "old-browse", basePageKey: "latest" })}>Browse</button>
      <button onClick={() => {
        const b = state.historyBrowse!;
        dispatch({ type: "install_history_browse_page", sid, scopeKey: b.scopeKey,
          revision: b.revision, generation: b.generation, viewId: b.viewId,
          windowEpoch: b.windowEpoch, before: b.olderCursor!, page: {
            pageKey: "older", turns: Array.from({ length: 12 }, (_, i) => turn(`older-${i}`)),
            hasOlder: false, olderCursor: null, newerPageKey: "latest",
          } });
      }}>Older page</button>
      <output data-testid="surface">{scope}</output>
      <output data-testid="browse-id">{state.historyBrowse?.viewId ?? "latest"}</output>
    </div>
    <ChatView key={`${scope}:${sid}`} {...memory} sid={sid} engine={engine}
      turns={view.turns} hasMore={view.hasMore} historyRevision={runtime.historyRevision}
      historyGeneration={runtime.historyGeneration} historyViewRevision={view.viewRevision}
      historyScopeKey={view.scopeKey ?? scope} historyViewId={view.viewId}
      historyWindowEpoch={view.windowEpoch} historyCursor={view.oldestId}
      browseMode={view.browsing} hasNewer={view.hasNewer}
      onEdit={() => {}} onGetDiff={() => {}} />
  </main>;
}
