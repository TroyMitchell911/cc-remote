import type { SessionInfo } from "./protocol";

export const HISTORY_REQUEST_TIMEOUT_MS = 15_000;
export const HISTORY_DETAIL_REQUEST_TIMEOUT_MS = 30_000;

export interface HistoryRequestKey {
  sid: string;
  before?: string | null;
  limit: number;
  generation?: string | null;
  revision?: string | null;
  browse?: HistoryBrowseRequestContext;
}

/** Request-time authority for one display-only deep-history waiter.
 *
 * This metadata is deliberately local: protocol v21 already identifies the
 * immutable server page with sid/revision/generation/before.  Freezing the
 * browser view here prevents a delayed page from being relabelled with the
 * machine/surface/session that happens to be active when it arrives.
 */
export interface HistoryBrowseRequestContext {
  scopeKey: string;
  viewId: string;
  windowEpoch: number;
  pendingBefore: string;
  sourcePageKey: string | null;
  anchorTurnId?: string | null;
}

function sameBrowseWaiter(
  left: HistoryBrowseRequestContext,
  right: HistoryBrowseRequestContext,
): boolean {
  return left.scopeKey === right.scopeKey
    && left.viewId === right.viewId
    && left.windowEpoch === right.windowEpoch
    && left.pendingBefore === right.pendingBefore
    && left.sourcePageKey === right.sourcePageKey
    && (left.anchorTurnId ?? null) === (right.anchorTurnId ?? null);
}

/** Resolve an optional acceleration hint from ownership-accepted session lists.
 *
 * The wrapper remains authoritative. If two accepted engine/space scopes ever
 * claim the same id with different directories, omit the hint and let the SDK
 * perform its global session lookup.
 */
export function resolveHistoryCwdHint(
  lists: Readonly<Record<string, readonly SessionInfo[]>>,
  sid: string,
): string | undefined {
  const hints = new Set<string>();
  for (const sessions of Object.values(lists)) {
    const cwd = sessions.find((session) => session.session_id === sid)?.cwd?.trim();
    if (cwd) hints.add(cwd);
  }
  return hints.size === 1 ? hints.values().next().value : undefined;
}

interface PendingHistoryRequest extends HistoryRequestKey {
  connectionEpoch: number;
  startedAt: number;
  browseWaiters: HistoryBrowseRequestContext[];
}

interface RetiredHistoryRequest {
  sid: string;
  before?: string | null;
  generation?: string | null;
  revision?: string | null;
}

// RelayWs can retain this many reliable commands. Keep the same bounded number
// of response authorities: a transcript scan may legitimately outlive several
// ordinary request timeouts, especially on a phone reconnecting over a slow
// uplink. Time-based expiry would then let a delayed old page consume the exact
// same-cursor request issued by the replacement connection.
const MAX_RETIRED_HISTORY_REQUESTS = 256;

export interface HistoryRequestCompletion {
  matched: HistoryBrowseRequestContext[];
  stale: HistoryBrowseRequestContext[];
}

export interface CancelledHistoryBrowseRequest {
  sid: string;
  generation?: string | null;
  revision: string;
  browse: HistoryBrowseRequestContext;
}

/** One authority for every focus/reconnect/rebuild history trigger.
 *
 * App historically had four independent call sites.  Each generated a new
 * reliable command id, so wrapper-side command dedupe could not recognize that
 * they all requested the same page.  This coordinator merges those triggers
 * within a connection while still allowing a newer rollback revision, wrapper
 * generation, or pagination cursor to issue its own read.
 */
export class HistoryRequestCoordinator {
  private connectionEpoch = 0;
  private readonly pending = new Map<string, PendingHistoryRequest>();
  private readonly retired: RetiredHistoryRequest[] = [];
  private readonly now: () => number;
  private readonly timeoutMs: number;

  constructor(
    now: () => number = () => Date.now(),
    timeoutMs = HISTORY_REQUEST_TIMEOUT_MS,
  ) {
    this.now = now;
    this.timeoutMs = timeoutMs;
  }

  beginConnection(): CancelledHistoryBrowseRequest[] {
    const cancelled = this.retirePending();
    this.connectionEpoch += 1;
    return cancelled;
  }

  clear(): CancelledHistoryBrowseRequest[] {
    const cancelled = this.cancelledBrowseWaiters();
    this.pending.clear();
    this.retired.length = 0;
    return cancelled;
  }

  private cancelledBrowseWaiters(
    requests: Iterable<PendingHistoryRequest> = this.pending.values(),
  ): CancelledHistoryBrowseRequest[] {
    const cancelled: CancelledHistoryBrowseRequest[] = [];
    for (const pending of requests) {
      if (!pending.revision) continue;
      for (const browse of pending.browseWaiters) {
        cancelled.push({
          sid: pending.sid,
          generation: pending.generation,
          revision: pending.revision,
          browse: { ...browse },
        });
      }
    }
    return cancelled;
  }

  private boundRetired(): void {
    if (this.retired.length > MAX_RETIRED_HISTORY_REQUESTS) {
      this.retired.splice(
        0,
        this.retired.length - MAX_RETIRED_HISTORY_REQUESTS,
      );
    }
  }

  private retirePending(
    requests: Iterable<PendingHistoryRequest> = this.pending.values(),
  ): CancelledHistoryBrowseRequest[] {
    const retained = [...requests];
    const cancelled = this.cancelledBrowseWaiters(retained);
    for (const pending of retained) {
      this.retired.push({
        sid: pending.sid,
        before: pending.before,
        generation: pending.generation,
        revision: pending.revision,
      });
    }
    this.pending.clear();
    this.boundRetired();
    return cancelled;
  }

  private static key(request: HistoryRequestKey): string {
    return `${request.sid}\u0000${request.before ?? ""}\u0000${request.limit}`;
  }

  request(
    request: HistoryRequestKey,
    send: () => boolean,
    onCancelled: (cancelled: CancelledHistoryBrowseRequest[]) => void =
      () => undefined,
  ): boolean {
    const key = HistoryRequestCoordinator.key(request);
    const existing = this.pending.get(key);
    const now = this.now();
    if (existing && existing.connectionEpoch === this.connectionEpoch
        && now - existing.startedAt < this.timeoutMs) {
      // A newly revealed destructive revision must issue a replacement even
      // when an ordinary focus read is already in flight.  The reverse is safe:
      // a later generic reconnect trigger can share the revision-bound read.
      const sameRevision = existing.revision
        ? !request.revision || existing.revision === request.revision
        : !request.revision;
      // A generic focus read already on the wire cannot satisfy a replay which
      // has since revealed its exact wrapper generation. Send a replacement
      // instead of relabelling the old request: its response may belong to the
      // previous generation and will be rejected by the reducer. The reverse
      // remains safe because a generic trigger can share a generation-bound
      // request already in flight.
      const sameGeneration = existing.generation
        ? !request.generation || existing.generation === request.generation
        : !request.generation;
      if (sameRevision && sameGeneration) {
        if (request.browse) {
          const duplicate = existing.browseWaiters.some((waiter) =>
            sameBrowseWaiter(waiter, request.browse!));
          if (!duplicate) existing.browseWaiters.push({ ...request.browse });
          // The local anchor has a real waiter even though the immutable wire
          // page was already in flight.
          return !duplicate;
        }
        return false;
      }
    }
    const pending: PendingHistoryRequest = {
      ...request,
      connectionEpoch: this.connectionEpoch,
      startedAt: now,
      browseWaiters: request.browse ? [{ ...request.browse }] : [],
    };
    // Outbox saturation/disconnection is a real rejection. Do not leave a
    // phantom pending entry which suppresses the user's next pagination
    // attempt while no command exists on the wire.
    if (!send()) return false;
    if (existing) {
      const cancelled = this.cancelledBrowseWaiters([existing]).filter(
        (candidate) => !pending.browseWaiters.some(
          (waiter) => sameBrowseWaiter(candidate.browse, waiter)),
      );
      this.retired.push({
        sid: existing.sid,
        before: existing.before,
        generation: existing.generation,
        revision: existing.revision,
      });
      this.boundRetired();
      if (cancelled.length > 0) onCancelled(cancelled);
    }
    this.pending.set(key, pending);
    return true;
  }

  complete(response: {
    session_id: string;
    before?: string | null;
    generation?: string | null;
    revision?: string | null;
  }): HistoryRequestCompletion {
    const matched: HistoryBrowseRequestContext[] = [];
    const stale: HistoryBrowseRequestContext[] = [];
    let retiredMatch = -1;
    for (let index = this.retired.length - 1; index >= 0; index -= 1) {
      const retired = this.retired[index];
      if (retired.sid !== response.session_id
          || (retired.before ?? "") !== (response.before ?? "")
          || (retired.generation
            && retired.generation !== response.generation)
          || (retired.revision
            && retired.revision !== response.revision)) continue;
      retiredMatch = index;
      break;
    }
    let matchedActive: PendingHistoryRequest | null = null;
    const mismatched: [string, PendingHistoryRequest][] = [];
    for (const [key, pending] of this.pending) {
      if (pending.sid !== response.session_id
          || (pending.before ?? "") !== (response.before ?? "")) continue;
      if ((pending.generation
            && pending.generation !== response.generation)
          || (pending.revision
            && pending.revision !== response.revision)) {
        mismatched.push([key, pending]);
        continue;
      }
      matchedActive = pending;
      matched.push(...pending.browseWaiters.map((waiter) => ({ ...waiter })));
      this.pending.delete(key);
    }
    if (retiredMatch >= 0) {
      if (matchedActive) {
        // The response can render the active waiter, but without a wire request
        // id we cannot know whether it answered that request or its delayed
        // predecessor. Keep one conservative tombstone for the still-possible
        // response: a field remains constrained only when both requests agree.
        const retired = this.retired[retiredMatch];
        this.retired[retiredMatch] = {
          sid: retired.sid,
          before: retired.before,
          generation: retired.generation === matchedActive.generation
            ? retired.generation : undefined,
          revision: retired.revision === matchedActive.revision
            ? retired.revision : undefined,
        };
      } else {
        // One response accounts for exactly one retired wire request. Preserve
        // duplicate tombstones so a second delayed response cannot consume a
        // newer same-cursor request later.
        this.retired.splice(retiredMatch, 1);
      }
    }
    // Only an otherwise-unattributable response proves that the active browse
    // request itself crossed a revision/generation boundary. A response which
    // matches a retired request is delayed old work and must leave its exact
    // same-cursor replacement untouched.
    if (!matchedActive && retiredMatch < 0) {
      for (const [key, pending] of mismatched) {
        if (pending.browseWaiters.length === 0) continue;
        stale.push(...pending.browseWaiters.map((waiter) => ({ ...waiter })));
        this.pending.delete(key);
      }
    }
    return { matched, stale };
  }

  size(): number {
    return this.pending.size;
  }
}

export type HistoryDetailRequestContext =
  | {
      target: "runtime";
      scopeKey: string;
      sid: string;
      revision: string;
      turnId: string;
      before?: string | null;
      /** Initial user expansion pages to EOF; refresh repair reads one page. */
      autoLoad?: boolean;
    }
  | {
      target: "browse";
      scopeKey: string;
      sid: string;
      revision: string;
      turnId: string;
      before?: string | null;
      viewId: string;
      /** Diagnostic request-time page epoch. Detail remains valid after safe
       * pagination inside the same scope/view/revision while the row exists. */
      windowEpoch: number;
    };

/** Correlate protocol-v21 TurnDetail responses with the projection that asked.
 *
 * TurnDetail has no request id. The wire key is nevertheless exact because one
 * revision contains at most one canonical detail row per sid/turn. Keeping the
 * target frozen locally prevents a response requested in browse mode from
 * mutating the live runtime after navigation.
 */
export class HistoryDetailRequestCoordinator {
  private readonly pending = new Map<string, {
    contexts: HistoryDetailRequestContext[];
    timer: unknown;
  }>();
  private readonly onTimeout: (context: HistoryDetailRequestContext) => void;
  private readonly schedule: (
    callback: () => void,
    delayMs: number,
  ) => unknown;
  private readonly cancelTimer: (timer: unknown) => void;
  private readonly timeoutMs: number;

  constructor(
    onTimeout: (context: HistoryDetailRequestContext) => void =
      () => undefined,
    schedule: (callback: () => void, delayMs: number) => unknown =
      (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    cancelTimer: (timer: unknown) => void =
      (timer) => globalThis.clearTimeout(
        timer as ReturnType<typeof globalThis.setTimeout>),
    timeoutMs = HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  ) {
    this.onTimeout = onTimeout;
    this.schedule = schedule;
    this.cancelTimer = cancelTimer;
    this.timeoutMs = timeoutMs;
  }

  private static key(input: {
    session_id?: string;
    sid?: string;
    revision: string;
    turn_id?: string;
    turnId?: string;
    before?: string | null;
  }): string {
    return `${input.session_id ?? input.sid ?? ""}\u0000${input.revision}`
      + `\u0000${input.turn_id ?? input.turnId ?? ""}`
      + `\u0000${input.before ?? ""}`;
  }

  private static contextKey(context: HistoryDetailRequestContext): string {
    return context.target === "browse"
      ? [context.target, context.scopeKey, context.viewId, context.windowEpoch]
        .join("\u0000")
      : [context.target, context.scopeKey, context.autoLoad ? 1 : 0]
        .join("\u0000");
  }

  register(context: HistoryDetailRequestContext): {
    accepted: boolean;
    send: boolean;
  } {
    const key = HistoryDetailRequestCoordinator.key(context);
    const existing = this.pending.get(key);
    if (existing) {
      const contextKey = HistoryDetailRequestCoordinator.contextKey(context);
      if (!existing.contexts.some((candidate) =>
        HistoryDetailRequestCoordinator.contextKey(candidate) === contextKey)) {
        existing.contexts.push({ ...context });
      }
      return { accepted: true, send: false };
    }
    const pending = {
      contexts: [{ ...context }],
      timer: null as unknown,
    };
    this.pending.set(key, pending);
    try {
      pending.timer = this.schedule(() => {
        if (this.pending.get(key) !== pending) return;
        this.pending.delete(key);
        for (const target of pending.contexts) this.onTimeout({ ...target });
      }, this.timeoutMs);
    } catch {
      this.pending.delete(key);
      return { accepted: false, send: false };
    }
    return { accepted: true, send: true };
  }

  begin(context: HistoryDetailRequestContext): boolean {
    const registration = this.register(context);
    return registration.accepted && registration.send;
  }

  complete(response: {
    session_id: string;
    revision: string;
    turn_id: string;
    before?: string | null;
  }): HistoryDetailRequestContext | null {
    return this.completeAll(response)[0] ?? null;
  }

  completeAll(response: {
    session_id: string;
    revision: string;
    turn_id: string;
    before?: string | null;
  }): HistoryDetailRequestContext[] {
    const key = HistoryDetailRequestCoordinator.key(response);
    const pending = this.pending.get(key);
    if (!pending) return [];
    this.pending.delete(key);
    this.cancelTimer(pending.timer);
    return pending.contexts.map((context) => ({ ...context }));
  }

  cancel(context: HistoryDetailRequestContext): void {
    const key = HistoryDetailRequestCoordinator.key(context);
    const pending = this.pending.get(key);
    if (!pending) return;
    const contextKey = HistoryDetailRequestCoordinator.contextKey(context);
    pending.contexts = pending.contexts.filter((candidate) =>
      HistoryDetailRequestCoordinator.contextKey(candidate) !== contextKey);
    if (pending.contexts.length === 0) {
      this.pending.delete(key);
      this.cancelTimer(pending.timer);
    }
  }

  cancelTurn(input: {
    sid: string;
    revision: string;
    turnId: string;
  }): HistoryDetailRequestContext[] {
    const cancelled: HistoryDetailRequestContext[] = [];
    for (const [key, pending] of this.pending) {
      const retained = pending.contexts.filter((context) => {
        const matches = context.sid === input.sid
          && context.revision === input.revision
          && context.turnId === input.turnId;
        if (matches) cancelled.push({ ...context });
        return !matches;
      });
      if (retained.length === pending.contexts.length) continue;
      if (retained.length > 0) {
        pending.contexts = retained;
      } else {
        this.pending.delete(key);
        this.cancelTimer(pending.timer);
      }
    }
    return cancelled;
  }

  clear(): HistoryDetailRequestContext[] {
    const contexts = [...this.pending.values()].flatMap((pending) => {
      this.cancelTimer(pending.timer);
      return pending.contexts.map((context) => ({ ...context }));
    });
    this.pending.clear();
    return contexts;
  }
}
