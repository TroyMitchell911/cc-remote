import { previewUrl } from "./web-link-url";

export interface LinkMetadata { title?: string; description?: string; icon?: string; image?: string }

const cache = new Map<string, { expires: number; value: LinkMetadata }>();
const pending = new Map<string, Promise<LinkMetadata>>();
const iconListeners = new Map<string, Set<(icon: string) => void>>();
const queue: (() => void)[] = [];
let active = 0;
const imageData = (value: unknown): string | undefined => typeof value === "string"
  && value.length <= 360000
  && /^data:image\/(?:png|jpeg|gif|webp|x-icon);base64,[A-Za-z0-9+/=]+$/.test(value) ? value : undefined;

function remember(key: string, value: LinkMetadata) {
  cache.delete(key);
  const complete = !!value.icon && (key.startsWith("icon:") || !!value.image);
  cache.set(key, { expires: Date.now() + (complete ? 3600000 : 60000), value });
  while (cache.size > 64) cache.delete(cache.keys().next().value!);
}

export function subscribeLinkIcon(href: string, listener: (icon: string) => void): () => void {
  const origin = previewUrl(href)?.origin;
  if (!origin) return () => {};
  let listeners = iconListeners.get(origin);
  if (!listeners) iconListeners.set(origin, listeners = new Set());
  listeners.add(listener);
  const hit = cache.get(`icon:${origin}`);
  if (hit && hit.expires > Date.now() && hit.value.icon) listener(hit.value.icon);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) iconListeners.delete(origin);
  };
}

export function getLinkMetadata(href: string, detail: "icon" | "page"): Promise<LinkMetadata> {
  const url = previewUrl(href);
  if (!url) return Promise.resolve({});
  const value = detail === "icon" ? url.origin : url.href;
  const key = `${detail}:${value}`;
  const now = Date.now();
  for (const [oldKey, entry] of cache) if (entry.expires < now) cache.delete(oldKey);
  const hit = cache.get(key);
  if (hit) { cache.delete(key); cache.set(key, hit); return Promise.resolve(hit.value); }
  const inflight = pending.get(key);
  if (inflight) return inflight;
  if (pending.size >= 64) return Promise.resolve({});
  const promise = new Promise<LinkMetadata>((resolve) => {
    const run = async () => {
      active++;
      let result: LinkMetadata = {};
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 14000);
      try {
        const response = await fetch("/api/link-preview", {
          method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: value, detail }),
        });
        if (response.ok) {
          const data: unknown = await response.json();
          if (data && typeof data === "object") {
            const fields = data as Record<string, unknown>;
            result = {
              title: typeof fields.title === "string" ? fields.title.slice(0, 240) : undefined,
              description: typeof fields.description === "string" ? fields.description.slice(0, 400) : undefined,
              icon: imageData(fields.icon), image: imageData(fields.image),
            };
          }
        }
      } catch { /* A missing preview must never prevent opening the link. */ }
      finally {
        clearTimeout(timer);
        const iconKey = `icon:${url.origin}`;
        const known = cache.get(iconKey);
        if (detail === "icon" && !result.icon && known && known.expires > Date.now()) result = known.value;
        remember(key, result);
        if (result.icon) {
          remember(iconKey, { icon: result.icon });
          iconListeners.get(url.origin)?.forEach((listener) => listener(result.icon!));
        }
        pending.delete(key);
        active--;
        resolve(result);
        queue.shift()?.();
      }
    };
    if (active < 3) void run();
    else if (detail === "page") queue.unshift(() => { void run(); });
    else queue.push(() => { void run(); });
  });
  pending.set(key, promise);
  return promise;
}
