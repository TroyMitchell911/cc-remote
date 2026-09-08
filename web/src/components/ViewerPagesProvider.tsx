import { lazy, Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import { viewerScopeKey, type ViewerPage, type ViewerScope } from "../remote-viewer";
import { ViewerPagesContext, type PagesContext } from "../viewer-pages-context";
import { ViewerPagesCache } from "../viewer-pages-cache";

const Controller = lazy(() => import("../viewer-pages-controller"));

export function ViewerPagesProvider({ scope, onOpen, children }: {
  scope: ViewerScope | null; onOpen: (page: ViewerPage) => void; children: ReactNode;
}) {
  const key = scope ? viewerScopeKey(scope) : "";
  const stableScope = useMemo<ViewerScope | null>(() => {
    if (!key) return null;
    const [machineId, space, engine, sid] = JSON.parse(key);
    return { machineId, space, engine, sid };
  }, [key]);
  const [snapshot, setSnapshot] = useState<PagesContext | null>(null);
  const [cache] = useState(() => new ViewerPagesCache());
  useEffect(() => { if (!stableScope) cache.clear(); }, [cache, stableScope]);
  // Restore display metadata synchronously, before the lazy controller's
  // effect publishes live callbacks. Opening still obtains a fresh grant.
  const restored = useMemo<PagesContext | null>(() => {
    if (!stableScope) return null;
    const pages = cache.get(stableScope) ?? [];
    const pending = async (): Promise<never> => { throw new Error("页面信息正在加载，请稍后重试。"); };
    return { scope: stableScope, pages, loading: true, error: null, commandsReady: false,
      refresh: pending, associate: pending, remove: pending, discover: () => {},
      open: onOpen, openLink: (href) => {
        const matches = pages.filter((page) => page.available && page.references.includes(href));
        if (matches.length !== 1) return false;
        onOpen(matches[0]);
        return true;
      } };
  }, [stableScope, cache, onOpen]);
  // Only the metadata controller remounts on focus change, NEVER the chat or
  // its DOM/scroll/selection state. Deferred requests retain their old scope.
  return <ViewerPagesContext.Provider value={snapshot && viewerScopeKey(snapshot.scope) === key ? snapshot : restored}>
    {stableScope && <Suspense fallback={null}>
      <Controller key={key} scope={stableScope} cache={cache} onOpen={onOpen} onChange={setSnapshot} />
    </Suspense>}
    {children}
  </ViewerPagesContext.Provider>;
}
