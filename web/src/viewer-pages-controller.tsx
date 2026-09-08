import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ViewerPage, ViewerScope } from "./remote-viewer";
import { viewerRequest } from "./viewer-request";
import type { PagesContext } from "./viewer-pages-context";
import { ViewerDiscoveryQueue } from "./viewer-discovery";
import type { ViewerPagesCache } from "./viewer-pages-cache";

export default function ViewerPagesController({ scope, cache, onOpen, onChange }: {
  cache: ViewerPagesCache;
  scope: ViewerScope; onOpen: (page: ViewerPage) => void; onChange: (value: PagesContext | null) => void;
}) {
  const [pages, setPages] = useState<ViewerPage[]>(() => cache.get(scope) ?? []);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const live = useRef(true);
  useEffect(() => () => onChange(null), [onChange]);
  const serial = useRef(0);
  const wireScope = useMemo(() => ({ machine_id: scope.machineId, sid: scope.sid, engine: scope.engine, space: scope.space }),
    [scope.machineId, scope.sid, scope.engine, scope.space]);
  const discovery = useRef(new ViewerDiscoveryQueue());
  const timer = useRef<number | undefined>(undefined);
  const draining = useRef(false);
  const drainRef = useRef<() => void>(() => {});
  const schedule = useCallback(() => {
    window.clearTimeout(timer.current);
    const due = discovery.current.nextAt();
    if (live.current && !draining.current && Number.isFinite(due))
      timer.current = window.setTimeout(() => drainRef.current(), Math.max(80, due - Date.now()));
  }, []);
  const request = useCallback(async (action: string, payload = {}): Promise<ViewerPage[]> => {
    const sequence = ++serial.current;
    const result = await viewerRequest<{ pages: ViewerPage[] }>("/api/viewers/pages", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: wireScope, action, ...payload }), signal: AbortSignal.timeout(30000),
    });
    if (!live.current) throw new Error("预览会话已切换。");
    if (!Array.isArray(result.pages)) throw new Error("页面列表暂不可用。");
    if (sequence === serial.current) { cache.set(scope, result.pages); setPages(result.pages); setError(null); }
    // A newer list/remove can supersede a discovery response. Do not confirm
    // a hint whose result was never painted; retry against the current state.
    else if (action === "resolve") return [];
    return result.pages;
  }, [wireScope, cache, scope]);
  const refresh = useCallback(() => request("list"), [request]);
  useEffect(() => {
    live.current = true;
    let retryTimer: number | undefined;
    let retryDelay = 2000;
    let reading = false;
    const read = () => {
      if (draining.current || reading || !live.current) return;
      reading = true;
      window.clearTimeout(retryTimer);
      void refresh().then(() => { retryDelay = 2000; }).catch((reason: unknown) => {
        if (live.current) {
          setError(reason instanceof Error ? reason.message : "页面列表暂不可用。");
          retryTimer = window.setTimeout(read, retryDelay);
          retryDelay = Math.min(30000, retryDelay * 2);
        }
      }).finally(() => { reading = false; if (live.current) setLoading(false); });
    };
    read();
    const poll = window.setInterval(() => { if (document.visibilityState === "visible") read(); }, 30000);
    return () => { live.current = false; window.clearInterval(poll); window.clearTimeout(timer.current); window.clearTimeout(retryTimer); };
  }, [refresh]);
  const drain = useCallback(async () => {
    if (draining.current || !live.current) return;
    draining.current = true;
    try {
      while (live.current) {
        const batch = discovery.current.take(Date.now());
        if (!batch) break;
        try {
          const values = await request("resolve", { paths: batch.paths, turn_id: batch.turnId });
          discovery.current.settle(batch, values, Date.now());
        } catch {
          // Bounded retry covers delayed files and transient metadata failures,
          // even if no further text arrives. Never paint unverified hints.
          discovery.current.settle(batch, [], Date.now());
        }
      }
    } finally { draining.current = false; schedule(); }
  }, [request, schedule]);
  drainRef.current = () => void drain();
  const discover = useCallback((paths: string[], turnId: string, done = false) => {
    discovery.current.add(paths, turnId, done, Date.now());
    schedule();
  }, [schedule]);
  const associate = useCallback(async (page: { machine_id: string; site_id: string; entry: string }) => {
    const values = await request("associate", { page });
    return values.find((p) => p.machine_id === page.machine_id && p.site_id === page.site_id && p.entry === page.entry);
  }, [request]);
  const remove = useCallback(async (id: string) => { await request("remove", { page_id: id }); }, [request]);
  const openLink = useCallback((href: string) => {
    const matches = pages.filter((page) => page.available && page.references.includes(href));
    if (matches.length !== 1) return false;
    onOpen(matches[0]);
    return true;
  }, [pages, onOpen]);
  const value = useMemo(() => ({ scope, pages, error, loading, commandsReady: true, refresh, discover, associate, remove, open: onOpen, openLink }),
    [scope, pages, error, loading, refresh, discover, associate, remove, onOpen, openLink]);
  useEffect(() => onChange(value), [value, onChange]);
  return null;
}
