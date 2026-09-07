import { createContext, useCallback, useEffect, useRef } from "react";
import { isRegisteredViewerLink, type ViewerCatalog } from "./remote-viewer";

// true consumes the click; false preserves the anchor's native browser action.
export const RemoteViewerContext = createContext<((href: string) => boolean) | null>(null);

/** Catalog hints only, never grants/resources. Resolve before the click so an
 * unavailable Viewer cannot turn a normal link into an async/popup-blocked hop. */
export function useRemoteViewerLinks(scopeKey: string | null, open: (href: string) => void) {
  const catalog = useRef<{ scope: string; expires: number; value: ViewerCatalog } | null>(null);
  useEffect(() => {
    catalog.current = null;
    if (!scopeKey) return;
    let disposed = false;
    let request: AbortController | null = null;
    let timeout: number | undefined;
    const refresh = () => {
      if (disposed || request || document.visibilityState === "hidden") return;
      catalog.current = null;
      const controller = new AbortController();
      request = controller;
      timeout = window.setTimeout(() => controller.abort(), 10000);
      void import("./viewer-request").then(({ viewerRequest }) =>
        viewerRequest<ViewerCatalog>("/api/viewers", { signal: controller.signal }))
        .then((value) => {
          if (!disposed && !controller.signal.aborted) {
            catalog.current = { scope: scopeKey, expires: performance.now() + 45000, value };
          }
        }).catch(() => { /* Unknown/disabled catalogs must leave normal links alone. */ })
        .finally(() => { window.clearTimeout(timeout); request = null; });
    };
    const visibility = () => {
      catalog.current = null;
      if (document.visibilityState === "visible") refresh();
    };
    const focus = () => {
      // Moving focus out of the Viewer iframe can precede a link's click.
      // Don't discard a fresh catalog during that same pointer gesture.
      if (!catalog.current || catalog.current.expires <= performance.now()) refresh();
    };
    refresh();
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      request?.abort();
      window.clearTimeout(timeout);
      window.clearInterval(timer);
      window.removeEventListener("focus", focus);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [scopeKey]);
  return useCallback((href: string): boolean => {
    const current = catalog.current;
    if (!scopeKey || current?.scope !== scopeKey || current.expires <= performance.now()
        || document.visibilityState === "hidden" || !isRegisteredViewerLink(current.value, href)) return false;
    open(href);
    return true;
  }, [scopeKey, open]);
}
