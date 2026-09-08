import type { ViewerPage } from "./remote-viewer.ts";

const RETRY_DELAYS = [500, 1500, 4000];
interface Hint {
  path: string; turnId: string; done: boolean; confirmed: boolean;
  attempts: number; due: number; version: number; running: boolean;
}
export interface DiscoveryBatch { turnId: string; paths: string[]; versions: number[] }

/** One bounded queue per focused session. No speculative page associations. */
export class ViewerDiscoveryQueue {
  private hints = new Map<string, Hint>();

  add(paths: string[], turnId: string, done: boolean, now: number) {
    for (const path of paths) {
      const key = `${turnId}\0${path}`;
      const hint = this.hints.get(key);
      if (!hint) {
        if (this.hints.size >= 2048) continue;
        this.hints.set(key, { path, turnId, done, confirmed: false,
          attempts: 0, due: now, version: 0, running: false });
      } else if (done && !hint.done) {
        hint.done = true;
        if (!hint.confirmed) {
          // Do not lose this boundary when an earlier negative lookup is still
          // in flight. Its result cannot consume the new completion retry.
          hint.version++;
          hint.attempts = 0;
          hint.due = now;
        }
      }
    }
  }

  nextAt(): number {
    let next = Infinity;
    for (const hint of this.hints.values()) {
      if (!hint.confirmed && !hint.running) next = Math.min(next, hint.due);
    }
    return next;
  }

  take(now: number): DiscoveryBatch | undefined {
    let first: Hint | undefined;
    for (const hint of this.hints.values()) {
      if (!hint.confirmed && !hint.running && hint.due <= now && (!first || hint.due < first.due)) first = hint;
    }
    if (!first) return undefined;
    const batch: DiscoveryBatch = { turnId: first.turnId, paths: [], versions: [] };
    for (const hint of this.hints.values()) {
      if (hint.confirmed || hint.running || hint.due > now || batch.turnId !== hint.turnId) continue;
      hint.running = true;
      hint.attempts++;
      batch.paths.push(hint.path);
      batch.versions.push(hint.version);
      if (batch.paths.length === 8) break;
    }
    return batch;
  }

  settle(batch: DiscoveryBatch, pages: ViewerPage[], now: number) {
    batch.paths.forEach((path, index) => {
      const hint = this.hints.get(`${batch.turnId}\0${path}`)!;
      hint.running = false;
      if (pages.some((page) => page.available && page.references.includes(path))) {
        hint.confirmed = true;
        hint.due = Infinity;
      } else if (hint.version === batch.versions[index]) {
        const delay = RETRY_DELAYS[hint.attempts - 1];
        hint.due = delay === undefined ? Infinity : now + delay;
      }
    });
  }
}
