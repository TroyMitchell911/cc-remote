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

/** A child status/result is not evidence that the parent resumed execution. */
export function hasParentContinuation(blocks: Block[]): boolean {
  return blocks.some(block => block.kind === "tool"
    || block.kind === "text" && (!!block.text.trim() || !block.done) && block.delivery !== "async"
    || block.kind === "process" && ["command", "file_change", "mcp", "web_search", "server_tool", "reasoning"].includes(block.processKind));
}
