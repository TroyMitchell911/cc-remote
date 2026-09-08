/** Viewer descriptors only. Grants/cookies are never persisted in web storage. */
export interface ViewerSite {
  id: string;
  label: string;
  machine_id: string;
  entry: string;
  revision: string;
  urls: string[];
}
export interface ViewerSelection { machine_id: string; site_id: string; entry?: string }
export interface ViewerPage {
  id: string; machine_id: string; site_id: string; entry: string; label: string;
  references: string[]; turn_ids: string[]; available: boolean;
  revision?: string; content_revision?: string;
}
export interface ViewerScope { machineId: string; sid: string; space: string; engine: string }
export interface ViewerCatalog { enabled: boolean; sites: ViewerSite[] }
export const VIEWER_STORAGE = "cc-remote:viewer-panels-v1";

export function viewerScopeKey(scope: ViewerScope): string {
  return JSON.stringify([scope.machineId, scope.space, scope.engine, scope.sid]);
}

export function readViewerSelections(storage: Pick<Storage, "getItem">): Record<string, ViewerSelection | null> {
  try {
    const raw = storage.getItem(VIEWER_STORAGE);
    if (!raw || raw.length > 32768) return {};
    const data: unknown = JSON.parse(raw);
    if (!data || typeof data !== "object" || Array.isArray(data)) return {};
    const result: Record<string, ViewerSelection | null> = {};
    for (const [key, value] of Object.entries(data).slice(-32)) {
      let scope: unknown;
      try { scope = JSON.parse(key); } catch { continue; }
      if (!Array.isArray(scope) || scope.length !== 4
          || !scope.every((part) => typeof part === "string" && part.length > 0 && part.length <= 128)) continue;
      if (value === null) result[key] = null;
      else if (typeof value === "object" && !Array.isArray(value)
          && typeof value.machine_id === "string" && value.machine_id.length > 0 && value.machine_id.length <= 128
          && typeof value.site_id === "string" && /^[a-z0-9][a-z0-9-]{0,47}$/.test(value.site_id)) {
        result[key] = { machine_id: value.machine_id, site_id: value.site_id,
          ...(typeof value.entry === "string" && value.entry.startsWith("/")
            && value.entry.length <= 4096 && /\.html?$/i.test(value.entry) ? { entry: value.entry } : {}) };
      }
    }
    return result;
  } catch { return {}; }
}

export function setViewerSelection(values: Record<string, ViewerSelection | null>,
  key: string, selection: ViewerSelection | null): Record<string, ViewerSelection | null> {
  const rest = Object.entries(values).filter(([existing]) => existing !== key).slice(-31);
  return Object.fromEntries([...rest, [key, selection]]);
}

export function writeViewerSelections(storage: Pick<Storage, "setItem">,
  values: Record<string, ViewerSelection | null>) {
  try { storage.setItem(VIEWER_STORAGE, JSON.stringify(Object.fromEntries(Object.entries(values).slice(-32)))); }
  catch { /* Private browsing/quota must not disable the live panel. */ }
}

/** An address hint, never permission to contact the indicated host. */
export function isLocalViewerUrl(value: string): boolean {
  try {
    const url = new URL(value);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password) return false;
    const host = url.hostname;
    if (host === "localhost" || host === "[::1]" || host.endsWith(".local")) return true;
    const octets = host.split(".").map(Number);
    if (octets.length !== 4 || octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return false;
    return octets[0] === 10 || octets[0] === 127
      || (octets[0] === 192 && octets[1] === 168)
      || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
      || (octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127);
  } catch { return false; }
}

export function matchingViewers(sites: ViewerSite[], href: string): ViewerSite[] {
  try {
    const target = new URL(href);
    target.hash = "";
    target.search = "";
    return sites.filter((site) => site.urls.some((value) => {
      try { return new URL(value).href === target.href; } catch { return false; }
    }));
  } catch { return []; }
}

export function matchingViewer(sites: ViewerSite[], href: string): ViewerSite | null {
  const matches = matchingViewers(sites, href);
  // localhost is inherently ambiguous across machines. Never guess.
  return matches.length === 1 ? matches[0] : null;
}

export function isRegisteredViewerLink(catalog: ViewerCatalog | null, href: string): boolean {
  return catalog?.enabled === true && isLocalViewerUrl(href)
    && matchingViewers(catalog.sites, href).length > 0;
}
