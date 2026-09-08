import { useEffect, useState } from "react";

const loaders = {
  citation: () => import("./codex-file-citation").then((m) => m.remarkCodexFileCitations),
  details: () => import("./markdown-extras").then((m) => m.rehypeSafeDetails),
};
type Extra = keyof typeof loaders;
type Plugin = Awaited<ReturnType<typeof loaders[Extra]>>;
const loaded: Partial<Record<Extra, Plugin>> = {};

/** The test harness can preload both; production only loads the needed parser. */
export async function preloadMarkdownExtras(): Promise<void> {
  [loaded.citation, loaded.details] = await Promise.all([
    loaders.citation(), loaders.details(),
  ]);
}

function usePlugin(name: Extra, enabled: boolean): Plugin | null {
  const [plugin, setPlugin] = useState<Plugin | null>(() => loaded[name] ?? null);
  useEffect(() => {
    if (!enabled || plugin) return;
    let mounted = true;
    void loaders[name]().then((value) => {
      loaded[name] = value;
      if (mounted) setPlugin(() => value);
    }, () => {});
    return () => { mounted = false; };
  }, [name, enabled, plugin]);
  return enabled ? plugin : null;
}

export function useMarkdownExtras(source: string) {
  return {
    citation: usePlugin("citation", source.includes(":codex-file-citation{")),
    details: usePlugin("details", /<details(?:\s|>)/i.test(source)),
  };
}
