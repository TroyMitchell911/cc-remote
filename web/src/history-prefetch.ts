import type { History } from "./protocol";
import {
  HISTORY_REQUEST_TIMEOUT_MS,
  sameBrowseWaiter,
  type HistoryBrowseRequestContext,
  type HistoryRequestKey,
} from "./history-requests.ts";

/** One speculative summary page, scoped to the exact view that requested it.
 * Foreground reads still use HistoryRequestCoordinator, sharing an in-flight
 * prefetch rather than issuing another wire command. No timers or cascading
 * reads: only another upward gesture can request the next page. */
export class HistoryPagePrefetch {
  private entry: {
    key: string;
    request: HistoryRequestKey;
    startedAt: number;
    history: History | null;
  } | null = null;

  private key(request: HistoryRequestKey): string {
    return JSON.stringify([
      request.sid, request.before, request.generation ?? null, request.revision,
      request.browse?.scopeKey, request.browse?.viewId,
      request.browse?.windowEpoch,
    ]);
  }

  begin(request: HistoryRequestKey, now = Date.now()): boolean {
    const key = this.key(request);
    if (this.entry?.key === key && (this.entry.history
        || now - this.entry.startedAt < HISTORY_REQUEST_TIMEOUT_MS)) return false;
    this.entry = { key, request, startedAt: now, history: null };
    return true;
  }

  complete(history: History, waiters: readonly HistoryBrowseRequestContext[]): void {
    const entry = this.entry;
    if (!entry || history.authoritative === false || history.detail !== "summary"
        || history.session_id !== entry.request.sid
        || history.before !== entry.request.before
        || history.revision !== entry.request.revision
        || (entry.request.generation != null
          && history.generation !== entry.request.generation)
        || !entry.request.browse
        || !waiters.some(waiter => sameBrowseWaiter(waiter, entry.request.browse!))) return;
    entry.history = history;
  }

  take(request: HistoryRequestKey): History | null {
    if (this.entry?.key !== this.key(request)) return null;
    const history = this.entry.history;
    if (history) this.clear();
    return history;
  }

  clear(): void { this.entry = null; }
}
