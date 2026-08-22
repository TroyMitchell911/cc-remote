import type { Block, TextBlock, Turn } from "./domain/conversation";

/** Resolve runtime activity onto exactly one displayed narrative row. Session
 * state alone is deliberately insufficient: aliases can collide in migrated
 * caches, and a stale owner must not animate an unrelated historical turn. */
export function exactActiveTurnId(
  turns: readonly Pick<Turn, "id" | "clientMsgId" | "historyTurnId">[],
  ownerTurnId: string | null | undefined,
  active: boolean,
): string | null {
  const candidates = activeTurnCandidateIds(turns, ownerTurnId, active);
  return candidates.length === 1 ? candidates[0] : null;
}

/** Return only rows which exactly alias the active native/browser owner. An
 * empty result means inactive, absent from this projection, or unowned;
 * multiple results are the one ambiguity ChatView may resolve to a latest row. */
export function activeTurnCandidateIds(
  turns: readonly Pick<Turn, "id" | "clientMsgId" | "historyTurnId">[],
  ownerTurnId: string | null | undefined,
  active: boolean,
): string[] {
  if (!active || !ownerTurnId) return [];
  return turns.flatMap((turn) => [
    turn.id, turn.clientMsgId, turn.historyTurnId,
  ].includes(ownerTurnId) ? [turn.id] : []);
}

/** A newly submitted browser turn is an explicit, already-painted owner. It
 * must win over the prior native owner retained for late-event correlation;
 * otherwise the working spark briefly jumps back to the completed row until
 * the engine acknowledges and binds the new turn. */
export function displayActiveTurnOwnerId(
  liveOwnerTurnId: string | null | undefined,
  acceptancePending: string | null | undefined,
): string | null {
  return acceptancePending ?? liveOwnerTurnId ?? null;
}

export function processBlocks(blocks: Block[]): Block[] {
  const dedicatedAgents = new Set(blocks.flatMap((block) => (
    block.kind === "process" && block.processKind === "agent" && block.parent_id
      ? [block.parent_id] : []
  )));
  return blocks.filter((block) => {
    if (block.kind === "text") {
      return block.text.length > 0
        && (block.channel === "thinking" || block.channel === "commentary");
    }
    // Keep ToolUse in reducer state for result correlation and older peers,
    // while presenting the dedicated live agent lifecycle only once.
    if (block.kind === "tool"
        && (block.category === "agent"
          || ["agent", "task"].includes(block.tool.toLowerCase()))) {
      return !dedicatedAgents.has(block.tool_use_id);
    }
    return true;
  });
}

export function isCodexPresentationNoise(block: Block): boolean {
  if (block.kind === "text" && block.channel === "thinking") return true;
  if (block.kind !== "process") return false;
  if (block.processKind === "reasoning") return true;
  if (block.processKind !== "hook") return false;
  // Successful/pending hooks are plumbing around useful tool activity. Keep
  // only actionable hook failures in Codex's public process projection.
  return !["failed", "declined", "cancelled", "interrupted"].includes(
    block.status,
  );
}

export function presentableProcessBlocks(
  blocks: Block[],
  engine: "claude" | "codex",
): Block[] {
  const items = processBlocks(blocks);
  return engine === "codex"
    ? items.filter((block) => !isCodexPresentationNoise(block))
    : items;
}

export function finalTextBlocks(blocks: Block[]): TextBlock[] {
  return blocks.filter((block): block is TextBlock => block.kind === "text"
    && block.text.length > 0
    && (block.channel == null || block.channel === "final" || block.channel === "unknown"));
}

/** A main answer can finish before a background task or agent reports its
 * final lifecycle event. Keep the process shell live for those late updates
 * instead of presenting a running child as an already-completed turn. */
export function hasActiveProcess(blocks: Block[]): boolean {
  return blocks.some((block) =>
    (block.kind === "tool" || block.kind === "process") && !block.done);
}
