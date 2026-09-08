import type { HistoryImage } from "./protocol";

export type HistoryImageVariant = "thumbnail" | "full";

export interface HistoryImageAsset {
  status: "loading" | "ready" | "error";
  mediaType?: string;
  data?: string;
  width?: number;
  height?: number;
  error?: string;
  /** Wall-clock start of the active request. Present while loading so a
   * virtualized remount can keep the original timeout boundary. */
  startedAt?: number;
  /** Changes for every accepted begin(), including loading -> loading retries. */
  requestGeneration?: number;
}

interface AssetEntry extends HistoryImageAsset {
  sid: string;
  turnId: string;
  imageId: string;
  variant: HistoryImageVariant;
  lastUsed: number;
}

export interface HistoryImageRequest {
  sid: string;
  turnId: string;
  imageId: string;
  variant: HistoryImageVariant;
  requestId: string;
  revision?: string | null;
}

interface PendingAsset extends HistoryImageRequest {
  key: string;
  startedAt: number;
  requestGeneration: number;
}

export const MAX_HISTORY_IMAGE_ASSETS = 48;
export const HISTORY_IMAGE_REQUEST_TIMEOUT_MS = 15_000;

interface HistoryImageWakeScheduler {
  schedule(callback: () => void, delayMs: number): unknown;
  cancel(handle: unknown): void;
}

const defaultHistoryImageWakeScheduler: HistoryImageWakeScheduler = {
  schedule: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  cancel: (handle) => globalThis.clearTimeout(
    handle as ReturnType<typeof globalThis.setTimeout>,
  ),
};

let historyImageAssetCacheVersion = 0;
const historyImageAssetCacheListeners = new Set<() => void>();

export function subscribeHistoryImageAssetCacheChanges(
  listener: () => void,
): () => void {
  historyImageAssetCacheListeners.add(listener);
  return () => historyImageAssetCacheListeners.delete(listener);
}

export function historyImageAssetCacheSnapshot(): number {
  return historyImageAssetCacheVersion;
}

function publishHistoryImageAssetCacheChange(): void {
  historyImageAssetCacheVersion += 1;
  for (const listener of historyImageAssetCacheListeners) listener();
}

/** Initial viewport admission may be automatic. Once this mounted image has
 * observed cache residency, a later eviction needs an explicit user retry so
 * more visible images than the hard limit cannot continuously displace one
 * another. */
export function shouldAutoloadHistoryImage(
  asset: HistoryImageAsset | undefined,
  observedAsset: boolean,
): boolean {
  return !asset && !observedAsset;
}

export function historyImageAssetKey(
  turnId: string,
  imageId: string,
  variant: HistoryImageVariant,
): string {
  return `${turnId}\u0000${imageId}\u0000${variant}`;
}

/** Generated-image ids bind native task + content hash. Their public history
 * turn ids can change on refresh. Reuse ready originals in the caller's already
 * session-scoped, bounded cache; never alias uploads, pending requests, errors
 * or thumbnails, or retain bytes outside that cache. */
export function readyGeneratedImageAsset(
  assets: Record<string, HistoryImageAsset> | undefined,
  imageId: string,
): HistoryImageAsset | undefined {
  if (!assets) return undefined;
  const suffix = `\u0000${imageId}\u0000full`;
  return Object.entries(assets).find(([key, asset]) =>
    key.endsWith(suffix) && asset.status === "ready"
    && asset.data && asset.mediaType)?.[1];
}

/** Bounded in-memory cache for summary-page images. Summary history carries
 * metadata only; thumbnails enter this cache near the viewport and originals
 * on preview or for full-width generated output near the viewport. */
export class HistoryImageAssetCache {
  private readonly entries = new Map<string, AssetEntry>();
  private readonly pending = new Map<string, PendingAsset>();
  private readonly limit: number;
  private readonly now: () => number;
  private readonly wakeScheduler: HistoryImageWakeScheduler;
  private capacityWakeHandle: unknown = null;
  private capacityWakeAt: number | null = null;
  private tick = 0;
  private requestGeneration = 0;

  constructor(
    limit = MAX_HISTORY_IMAGE_ASSETS,
    now: () => number = () => Date.now(),
    wakeScheduler: HistoryImageWakeScheduler = defaultHistoryImageWakeScheduler,
  ) {
    this.limit = Math.max(0, Math.floor(limit));
    this.now = now;
    this.wakeScheduler = wakeScheduler;
  }

  private key(
    sid: string,
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ): string {
    return `${sid}\u0000${historyImageAssetKey(turnId, imageId, variant)}`;
  }

  /** Remove both halves of one request atomically. A pending response without
   * its entry must never be able to resurrect an LRU victim. */
  private removeKey(key: string): boolean {
    let changed = this.entries.delete(key);
    for (const [requestId, pending] of this.pending) {
      if (pending.key !== key) continue;
      this.pending.delete(requestId);
      changed = true;
    }
    return changed;
  }

  private cancelCapacityWake(): void {
    if (this.capacityWakeAt === null) return;
    this.wakeScheduler.cancel(this.capacityWakeHandle);
    this.capacityWakeHandle = null;
    this.capacityWakeAt = null;
  }

  private scheduleCapacityWake(now: number): void {
    let wakeAt = Number.POSITIVE_INFINITY;
    for (const entry of this.entries.values()) {
      if (entry.status !== "loading" || entry.startedAt == null) continue;
      wakeAt = Math.min(
        wakeAt,
        entry.startedAt + HISTORY_IMAGE_REQUEST_TIMEOUT_MS,
      );
    }
    if (!Number.isFinite(wakeAt)) return;
    if (this.capacityWakeAt !== null && this.capacityWakeAt <= wakeAt) return;
    this.cancelCapacityWake();
    this.capacityWakeAt = wakeAt;
    this.capacityWakeHandle = this.wakeScheduler.schedule(() => {
      this.capacityWakeHandle = null;
      this.capacityWakeAt = null;
      // Expiry changes admission even though no response mutated the maps.
      publishHistoryImageAssetCacheChange();
    }, Math.max(0, wakeAt - now));
  }

  begin(request: HistoryImageRequest): boolean {
    if (this.limit === 0 || this.pending.has(request.requestId)) return false;
    const key = this.key(
      request.sid, request.turnId, request.imageId, request.variant,
    );
    const now = this.now();
    const existing = this.entries.get(key);
    if (existing && (
      existing.status === "ready"
      || (existing.status === "loading"
        && now - (existing.startedAt ?? 0)
          < HISTORY_IMAGE_REQUEST_TIMEOUT_MS)
    )) return false;
    let changed = existing ? this.removeKey(key) : false;
    while (this.entries.size >= this.limit) {
      let oldestKey: string | null = null;
      let oldestTick = Number.POSITIVE_INFINITY;
      for (const [candidateKey, entry] of this.entries) {
        const activeLoading = entry.status === "loading"
          && now - (entry.startedAt ?? 0)
            < HISTORY_IMAGE_REQUEST_TIMEOUT_MS;
        if (activeLoading || entry.lastUsed >= oldestTick) continue;
        oldestKey = candidateKey;
        oldestTick = entry.lastUsed;
      }
      if (!oldestKey) {
        if (changed) publishHistoryImageAssetCacheChange();
        this.scheduleCapacityWake(now);
        return false;
      }
      changed = this.removeKey(oldestKey) || changed;
    }
    this.cancelCapacityWake();
    const requestGeneration = ++this.requestGeneration;
    this.entries.set(key, {
      sid: request.sid,
      turnId: request.turnId,
      imageId: request.imageId,
      variant: request.variant,
      status: "loading",
      lastUsed: ++this.tick,
      startedAt: now,
      requestGeneration,
    });
    this.pending.set(request.requestId, {
      ...request,
      key,
      startedAt: now,
      requestGeneration,
    });
    publishHistoryImageAssetCacheChange();
    return true;
  }

  has(
    sid: string,
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ): boolean {
    const entry = this.entries.get(this.key(sid, turnId, imageId, variant));
    if (!entry || entry.status === "error") return false;
    return entry.status === "ready"
      || this.now() - (entry.startedAt ?? 0)
        < HISTORY_IMAGE_REQUEST_TIMEOUT_MS;
  }

  cancel(requestId: string): void {
    const request = this.pending.get(requestId);
    if (!request) return;
    this.cancelCapacityWake();
    this.pending.delete(requestId);
    const entry = this.entries.get(request.key);
    if (entry?.status === "loading"
        && entry.requestGeneration === request.requestGeneration) {
      this.entries.delete(request.key);
    }
    publishHistoryImageAssetCacheChange();
  }

  accept(event: HistoryImage): boolean {
    const request = this.pending.get(event.request_id);
    if (!request
        || event.session_id !== request.sid
        || event.turn_id !== request.turnId
        || event.image_id !== request.imageId
        || event.variant !== request.variant
        // A correlated error can report the current revision precisely because
        // the request was stale. Consume it instead of leaving an endless spinner.
        || (request.revision != null && event.revision !== request.revision && !event.error)) {
      return false;
    }
    const current = this.entries.get(request.key);
    if (current?.status !== "loading"
        || current.requestGeneration !== request.requestGeneration) {
      this.pending.delete(event.request_id);
      return false;
    }
    this.cancelCapacityWake();
    this.pending.delete(event.request_id);
    const ready = !!event.data && !!event.media_type && !event.error;
    this.entries.set(request.key, {
      sid: request.sid,
      turnId: request.turnId,
      imageId: request.imageId,
      variant: request.variant,
      status: ready ? "ready" : "error",
      mediaType: ready ? event.media_type ?? undefined : undefined,
      data: ready ? event.data ?? undefined : undefined,
      width: event.width ?? undefined,
      height: event.height ?? undefined,
      error: ready ? undefined : event.error ?? "图片数据暂不可用，请重试",
      lastUsed: ++this.tick,
      requestGeneration: request.requestGeneration,
    });
    publishHistoryImageAssetCacheChange();
    return true;
  }

  forSession(sid: string): Record<string, HistoryImageAsset> {
    const assets: Record<string, HistoryImageAsset> = {};
    for (const entry of this.entries.values()) {
      if (entry.sid !== sid) continue;
      entry.lastUsed = ++this.tick;
      assets[historyImageAssetKey(entry.turnId, entry.imageId, entry.variant)] = {
        status: entry.status,
        mediaType: entry.mediaType,
        data: entry.data,
        width: entry.width,
        height: entry.height,
        error: entry.error,
        ...(entry.status === "loading"
          ? {
              startedAt: entry.startedAt,
              requestGeneration: entry.requestGeneration,
            }
          : {}),
      };
    }
    return assets;
  }

  dropSession(sid: string): boolean {
    let changed = false;
    for (const [key, entry] of this.entries) {
      if (entry.sid !== sid) continue;
      this.entries.delete(key);
      changed = true;
    }
    for (const [requestId, request] of this.pending) {
      if (request.sid !== sid) continue;
      this.pending.delete(requestId);
      changed = true;
    }
    if (changed) {
      this.cancelCapacityWake();
      publishHistoryImageAssetCacheChange();
    }
    return changed;
  }

  clear(): void {
    if (this.entries.size === 0 && this.pending.size === 0) return;
    this.cancelCapacityWake();
    this.entries.clear();
    this.pending.clear();
    publishHistoryImageAssetCacheChange();
  }
}
