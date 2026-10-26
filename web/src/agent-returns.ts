import type { Block, ProcessBlock } from "./domain/conversation";

/** Only native attribution establishes a return; titles/prose never do. */
export function isReturnReceipt(block: Block): boolean {
  return block.kind === "process" && block.phase === "end" && (
    block.server === "cc_remote" && ["agent_return", "task_return"].includes(block.tool ?? "")
    || block.processKind === "task" && block.server === "cc_remote_tasks" && block.tool === "task_result"
  );
}

/** Extra presentation metadata must never create a process/answer boundary. */
export function isSyntheticReturnReceipt(block: Block): boolean {
  return isReturnReceipt(block) && block.kind === "process" && block.server === "cc_remote"
    && (block.item_id.startsWith("return-") || block.item_id.startsWith("dsh-return-"));
}

export function returnedAgents(blocks: Block[]): ProcessBlock[] {
  return [...new Map(blocks.flatMap(block =>
    isReturnReceipt(block) && block.kind === "process" && returnRunId(block)
      ? [[returnRunId(block)!, block] as const] : [])).values()];
}

export function returnRunId(block: ProcessBlock): string | undefined {
  return block.processKind === "agent" && typeof block.input?.agent_run_id === "string"
    ? block.input.agent_run_id : undefined;
}

export function returnAnchorIds(block: Block): string[] {
  return block.kind === "text"
    ? [block.message_id, block.stream_message_id].filter((id): id is string => !!id)
      .map(id => `text:${id}`)
    : [block.kind === "tool" ? `tool:${block.tool_use_id}` : `process:${block.item_id}`];
}

/** Canonical summaries contain consumption order, whereas old live receipts
 * were emitted at task completion. Bind only metadata to an exact native block;
 * never reorder the parent's text/tools or infer causality from arrival time. */
export function anchorClaudeReturnReceipts(blocks: readonly Block[]): Block[] {
  return blocks.map((block, index) => {
    if (!isSyntheticReturnReceipt(block) || block.kind !== "process"
        || !block.item_id.startsWith("return-") || block.tool !== "agent_return"
        || block.returnAnchorId || block.liveOrder != null) return block;
    const next = blocks.slice(index + 1).find(candidate => hasParentContinuation([candidate]));
    return next ? { ...block, returnAnchorId: returnAnchorIds(next)[0] } : block;
  });
}

/** Return attribution is narrative metadata; child output remains in detail. */
export function compactReturnReceipt(block: ProcessBlock): ProcessBlock {
  return {
    kind: "process", item_id: block.item_id, processKind: block.processKind,
    phase: block.phase, status: block.status, done: block.done,
    title: block.title.slice(0, 4096), summary: block.summary?.slice(0, 4096),
    server: block.server, tool: block.tool, turn_id: block.turn_id,
    parent_id: block.parent_id, input: { agent_run_id: returnRunId(block) ?? null },
    background: block.background, startedTs: block.startedTs,
    updatedTs: block.updatedTs, terminalTs: block.terminalTs,
    liveOrder: block.liveOrder,
    returnAnchorId: block.returnAnchorId,
  };
}

/** Restore metadata omitted by a bounded detail window, using shared native
 * blocks as ordering anchors. Never infer a child from prose or timestamps. */
export function restoreReturnReceipts(target: readonly Block[], source: readonly Block[]): Block[] {
  const identity = (block: Block) => block.kind === "text" ? `text:${block.message_id}`
    : block.kind === "tool" ? `tool:${block.tool_use_id}` : `process:${block.item_id}`;
  const restored = [...target];
  const present = new Set(restored.map(identity));
  source.forEach((block, index) => {
    if (!isReturnReceipt(block) || block.kind !== "process" || present.has(identity(block))) return;
    const next = source.slice(index + 1).find(candidate => present.has(identity(candidate)));
    let at = next ? restored.findIndex(candidate => identity(candidate) === identity(next)) : restored.length;
    // Summary contains the final answer, while detail may add the preceding
    // detached thinking/tools. The link belongs at that same native continuation.
    if (next?.background === true) {
      while (at > 0) {
        const previous = restored[at - 1];
        if (previous.background !== true || isReturnReceipt(previous)
            || block.startedTs == null || previous.startedTs == null
            || previous.startedTs < block.startedTs
            || previous.kind === "text" && previous.channel === "final") break;
        at -= 1;
      }
    }
    restored.splice(at, 0, compactReturnReceipt(block));
    present.add(identity(block));
  });
  return restored;
}

/** A child status/result is not evidence that the parent resumed execution. */
export function hasParentContinuation(blocks: Block[]): boolean {
  return blocks.some(block => block.kind === "tool"
    || block.kind === "text" && (!!block.text.trim() || !block.done) && block.delivery !== "async"
    || block.kind === "process" && ["command", "file_change", "mcp", "web_search", "server_tool", "reasoning"].includes(block.processKind));
}
