import type { Action, AppState, Artifact } from "./reducer";

// In-memory, device-local reading state. Closing a preview still closes it;
// navigating away parks it under the parent conversation that owned the view.
const MAX_RETAINED_PREVIEWS = 8;
const ARTIFACT_EVENTS = new Set([
  "file_preview", "diff_report", "preview_asset", "file_save_result",
  "preview_authorization_required", "preview_authorization_result",
  "artifact_invalidated", "history_invalidated", "session_migrated",
  "rollback_result", "replay_start", "error",
]);

export function reduceWithRetainedArtifacts(
  state: AppState, action: Action,
  reduceCurrent: (state: AppState, action: Action) => AppState,
): AppState {
  const next = reduceCurrent(state, action);
  if (action.type === "reset") return next;
  const event = action.type === "event" ? action.event : null;
  const navigation = action.type === "focus_session"
    || action.type === "restore_session_list"
    || action.type === "clear_session_list"
    || action.type === "enter_new_chat"
    || event?.type === "session_focus";
  if (navigation) {
    const retained = { ...state.retainedArtifacts };
    if (state.focusedSid && state.artifact) {
      delete retained[state.focusedSid];
      retained[state.focusedSid] = state.artifact;
    }
    const sid = next.newChat ? null : next.focusedSid;
    const artifact = sid ? retained[sid] ?? null : null;
    if (sid) delete retained[sid];
    const entries = Object.entries(retained).slice(-MAX_RETAINED_PREVIEWS);
    return { ...next, artifact, retainedArtifacts: Object.fromEntries(entries) };
  }
  if (!event || !state.retainedArtifacts) return next;
  if (event.type === "session_rekey") {
    const retained: Record<string, Artifact> = {};
    for (const [sid, artifact] of Object.entries(state.retainedArtifacts)) {
      retained[sid === event.old_key ? event.session_id : sid] = artifact.sid === event.old_key
        ? { ...artifact, sid: event.session_id } : artifact;
    }
    return { ...next, retainedArtifacts: retained };
  }
  if (!ARTIFACT_EVENTS.has(event.type)) return next;
  let retained = state.retainedArtifacts;
  for (const [sid, artifact] of Object.entries(state.retainedArtifacts)) {
    // Reuse the existing exact request/session checks, including invalidation.
    // Only the artifact result is retained; lifecycle effects run once above.
    const updated = reduceCurrent({ ...state, artifact }, action).artifact;
    if (updated === artifact) continue;
    if (retained === state.retainedArtifacts) retained = { ...retained };
    if (updated) retained[sid] = updated;
    else delete retained[sid];
  }
  return retained === state.retainedArtifacts ? next : { ...next, retainedArtifacts: retained };
}
