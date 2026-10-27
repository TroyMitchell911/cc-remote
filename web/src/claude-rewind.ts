import type { Turn } from "./domain/conversation";
import type { ComposerDraftStore } from "./composer-drafts";
import type { RollbackTarget } from "./components/RollbackSheet";

export function rewindChoices(turns: Turn[]): RollbackTarget[] {
  const seen = new Set<string>();
  return turns.flatMap(turn => {
    if (!turn.checkpointId || !turn.done || seen.has(turn.checkpointId)) return [];
    seen.add(turn.checkpointId);
    return [{ checkpointId: turn.checkpointId, prompt: turn.prompt || "", ts: turn.ts }];
  });
}

/** A response from another tab or a repeated receipt must never replace a draft. */
export function prefillRewindDraft(store: ComposerDraftStore, key: string, prompt: string): boolean {
  const draft = store.get(key);
  if (draft.input || draft.images.length || draft.files.length || draft.pastes.length) return false;
  store.set(key, { ...draft, input: prompt });
  return true;
}
