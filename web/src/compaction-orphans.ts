import type { ProcessBlock, Turn } from "./domain/conversation";

function nativeTaskId(turn: Turn): string | undefined {
  return turn.liveTaskId ?? turn.forkPointId ?? turn.codexTurnId;
}

function exactAliases(turn: Turn): string[] {
  return [turn.id, turn.clientMsgId, turn.historyTurnId]
    .filter((value): value is string => !!value);
}

function orphanNativeId(
  turn: Turn,
  allowCompleted: boolean,
): string | undefined {
  if ((!allowCompleted && turn.done) || turn.prompt
      || turn.clientMsgId || turn.historyTurnId
      || turn.forkPointId || turn.checkpointId || turn.codexTurnId
      || turn.liveTaskId || turn.images?.length || turn.imageRefs?.length
      || turn.files?.length || turn.error || turn.interrupted
      || turn.detailProjection || turn.liveSpillBlocks?.length
      || turn.blocks.length === 0) return undefined;
  let nativeId: string | undefined;
  for (const block of turn.blocks) {
    if (block.kind !== "process" || block.processKind !== "compaction"
        || !block.turn_id || (nativeId && nativeId !== block.turn_id)) {
      return undefined;
    }
    nativeId = block.turn_id;
  }
  return nativeId;
}

function mergeLiveCompaction(owner: Turn, orphan: Turn): Turn | null {
  const source = orphan.blocks as ProcessBlock[];
  const incoming = source[0];
  const nativeId = incoming?.turn_id;
  const archive = owner.liveSpillBlocks ?? [];
  if (!incoming) return null;
  const observed = [
    ...owner.blocks,
    ...archive,
    ...(owner.detailProjection?.blocks ?? []),
  ].filter((block): block is ProcessBlock =>
    block.kind === "process" && block.processKind === "compaction");
  if (observed.some((block) => block.item_id === incoming.item_id)) {
    return owner;
  }
  // One native task can compact more than once. Without a cross-row order we
  // cannot tell where a distinct second marker belongs, so leave both rows for
  // authoritative History instead of deleting a real occurrence.
  if (observed.some((block) => block.turn_id === nativeId)) return null;
  const orders = [...archive, ...owner.blocks]
    .map((block) => block.liveOrder);
  const hasReliableOrder = orders.every(
    (order): order is number => Number.isFinite(order),
  ) && new Set(orders).size === orders.length;
  const shift = (
    blocks: readonly (typeof owner.blocks)[number][], fallbackStart: number,
  ) =>
    blocks.map((block, index) => ({
      ...block,
      liveOrder: hasReliableOrder
        ? block.liveOrder! + 1 : fallbackStart + index,
    }));
  const shiftedArchive = archive.length > 0 ? shift(archive, 1) : undefined;
  const shifted = shift(owner.blocks, archive.length + 1);
  return {
    ...owner,
    blocks: [{ ...incoming, liveOrder: 0 }, ...shifted],
    liveSpillBlocks: shiftedArchive,
    nextLiveBlockOrder: shifted.reduce(
      (maximum, block) => Math.max(maximum, block.liveOrder + 1),
      shiftedArchive?.reduce((maximum, block) =>
        Math.max(maximum, (block.liveOrder ?? -1) + 1), 1) ?? 1,
    ),
  };
}

export interface BoundCompactionOrphanReconciliation {
  turns: Turn[];
  owner: Turn | null;
  orphan: Turn | null;
}

export function reconcileBoundCompactionOrphanDetailed(
  turns: readonly Turn[],
  ownerAliases: readonly string[],
  nativeId: string | readonly string[],
): BoundCompactionOrphanReconciliation {
  const aliases = new Set(ownerAliases.filter(Boolean));
  const nativeIds = new Set(
    typeof nativeId === "string" ? [nativeId] : nativeId,
  );
  const owners = turns.flatMap((turn, index) =>
    exactAliases(turn).some((alias) => aliases.has(alias)) ? [index] : []);
  const orphans = turns.flatMap((turn, index) => {
    const orphanId = orphanNativeId(turn, false);
    return orphanId && nativeIds.has(orphanId) && turn.blocks.length === 1
      ? [index] : [];
  });
  if (owners.length !== 1 || orphans.length !== 1
      || owners[0] === orphans[0]) {
    return { turns: [...turns], owner: null, orphan: null };
  }
  const merged = mergeLiveCompaction(
    turns[owners[0]], turns[orphans[0]]);
  if (!merged) return { turns: [...turns], owner: null, orphan: null };
  const next = [...turns];
  next[owners[0]] = merged;
  next.splice(orphans[0], 1);
  return { turns: next, owner: merged, orphan: turns[orphans[0]] };
}

export function reconcileBoundCompactionOrphan(
  turns: readonly Turn[],
  ownerAliases: readonly string[],
  nativeId: string | readonly string[],
): Turn[] {
  return reconcileBoundCompactionOrphanDetailed(
    turns, ownerAliases, nativeId).turns;
}

export function reconcileProvenCompactionOrphans(
  turns: readonly Turn[],
): Turn[] {
  const owners = new Map<string, number[]>();
  const orphans = new Map<string, number[]>();
  turns.forEach((turn, index) => {
    const nativeId = nativeTaskId(turn);
    if (nativeId) owners.set(nativeId, [...owners.get(nativeId) ?? [], index]);
    const orphanId = orphanNativeId(turn, true);
    if (orphanId) orphans.set(orphanId,
      [...orphans.get(orphanId) ?? [], index]);
  });
  const next = [...turns];
  const removed = new Set<number>();
  next.forEach((owner) => {
    const nativeId = nativeTaskId(owner);
    const candidates = nativeId ? orphans.get(nativeId) : undefined;
    if (!nativeId || !owner.prompt || owners.get(nativeId)?.length !== 1
        || candidates?.length !== 1) return;
    const ownerItemIds = new Set(owner.blocks.flatMap((block) =>
      block.kind === "process" && block.processKind === "compaction"
        && block.turn_id === nativeId ? [block.item_id] : []));
    const orphan = next[candidates[0]];
    const orphanItemIds = orphan.blocks.flatMap((block) =>
      block.kind === "process" && block.processKind === "compaction"
        && block.turn_id === nativeId ? [block.item_id] : []);
    // A native task may compact repeatedly. The native turn id proves only the
    // owner task, not the occurrence, so remove a row only when every marker it
    // contains is already represented by the exact source-backed item id.
    if (orphanItemIds.length !== orphan.blocks.length
        || orphanItemIds.length === 0
        || !orphanItemIds.every((itemId) => ownerItemIds.has(itemId))) return;
    // The source-backed History owner is canonical. The cache/live orphan is
    // now proven to contain only exact duplicate occurrences; copying any of
    // its payload back would resurrect stale or duplicated compactions.
    removed.add(candidates[0]);
  });
  return next.filter((_, index) => !removed.has(index));
}
