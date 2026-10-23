import type { MermaidConfig } from "mermaid";
import { sanitizeSvgMarkup } from "./safe-svg";
import { MAX_MERMAID_SOURCE_CHARS, mermaidSourceProblem, type MermaidTheme } from "./mermaid";

const MAX_MERMAID_EDGES = 500;

function configForTheme(theme: MermaidTheme): MermaidConfig {
  return {
    startOnLoad: false,
    securityLevel: "strict",
    secure: [
      "secure",
      "securityLevel",
      "startOnLoad",
      "maxTextSize",
      "maxEdges",
      "htmlLabels",
      "dompurifyConfig",
      "theme",
      "themeVariables",
      "themeCSS",
      "fontFamily",
      "altFontFamily",
      "look",
      "layout",
      "suppressErrorRendering",
    ],
    suppressErrorRendering: true,
    htmlLabels: false,
    maxTextSize: MAX_MERMAID_SOURCE_CHARS,
    maxEdges: MAX_MERMAID_EDGES,
    theme: theme === "dark" ? "dark" : "neutral",
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
    logLevel: "fatal",
  };
}

type MermaidModule = typeof import("mermaid");
type DomPurifyModule = typeof import("dompurify");

let modulesPromise: Promise<{
  mermaid: MermaidModule["default"];
  DOMPurify: DomPurifyModule["default"];
}> | null = null;
let renderQueue: Promise<void> = Promise.resolve();
const renderedCache = new Map<string, string>();
const pendingRenders = new Map<string, Promise<string>>();
const MAX_CACHED_DIAGRAMS = 32;
const MAX_CACHED_CHARS = 2 * 1024 * 1024;
let cachedChars = 0;
let nextRenderId = 0;

function cacheKey(source: string, theme: MermaidTheme): string {
  return `${theme}\u0000${source}`;
}

function rememberDiagram(key: string, svg: string): void {
  const size = key.length + svg.length;
  if (size > MAX_CACHED_CHARS) return;
  renderedCache.set(key, svg);
  cachedChars += size;
  while (renderedCache.size > MAX_CACHED_DIAGRAMS || cachedChars > MAX_CACHED_CHARS) {
    const oldest = renderedCache.keys().next().value!;
    cachedChars -= oldest.length + renderedCache.get(oldest)!.length;
    renderedCache.delete(oldest);
  }
}

/** Cached SVGs must keep separate IDs in simultaneously mounted copies.
 * Namespace both definitions and references; keep sanitization ahead of caching. */
function scopedDiagram(svg: string, id: string): string {
  const document = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = document.documentElement;
  const ids = new Map<string, string>();
  for (const element of [root, ...root.querySelectorAll("[id]")]) {
    if (element.id) ids.set(element.id, `${id}-${ids.size}`);
  }
  const references = (value: string) => value.replace(
    /url\(\s*(['"]?)#([^\s)'" ]+)\1\s*\)/g,
    (original, _quote: string, target: string) =>
      ids.has(target) ? `url(#${ids.get(target)})` : original,
  );
  const selectorIds = new RegExp(`#(${[...ids.keys()]
    .sort((a, b) => b.length - a.length)
    .map((value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})(?![\\w-])`, "g");
  for (const element of [root, ...root.querySelectorAll("*")]) {
    for (const attribute of [...element.attributes]) {
      let value = attribute.value;
      if (attribute.name === "id") value = ids.get(value) ?? value;
      else if (["href", "xlink:href"].includes(attribute.name) && value.startsWith("#")) {
        value = ids.has(value.slice(1)) ? `#${ids.get(value.slice(1))}` : value;
      } else if (["aria-labelledby", "aria-describedby"].includes(attribute.name)) {
        value = value.split(/\s+/).map((target) => ids.get(target) ?? target).join(" ");
      } else value = references(value);
      if (value !== attribute.value) element.setAttribute(attribute.name, value);
    }
    if (element.localName === "style") {
      // Rewrite selectors separately so an ID resembling a hex color never
      // rewrites a declaration's color value.
      element.textContent = references(element.textContent ?? "").replace(
        /([^{}]+)\{/g,
        (_match, selector: string) => `${selector.replace(selectorIds,
          (_original, target: string) => `#${ids.get(target)}`)}{`,
      );
    }
  }
  return new XMLSerializer().serializeToString(root);
}

export function cachedMermaidSvg(source: string, id: string, theme: MermaidTheme): string | null {
  const key = cacheKey(source, theme);
  const svg = renderedCache.get(key);
  if (!svg) return null;
  renderedCache.delete(key);
  renderedCache.set(key, svg);
  return scopedDiagram(svg, id);
}

function loadModules() {
  modulesPromise ??= Promise.all([
    import("mermaid"),
    import("dompurify"),
  ]).then(([mermaidModule, domPurifyModule]) => ({
    mermaid: mermaidModule.default,
    DOMPurify: domPurifyModule.default,
  }));
  return modulesPromise;
}

function enqueueRender<T>(task: () => Promise<T>): Promise<T> {
  const result = renderQueue.then(task, task);
  renderQueue = result.then(() => undefined, () => undefined);
  return result;
}

export async function renderMermaidSvg(
  source: string,
  id: string,
  theme: MermaidTheme,
): Promise<string> {
  const problem = mermaidSourceProblem(source);
  if (problem) throw new Error(problem);
  const cached = cachedMermaidSvg(source, id, theme);
  if (cached) return cached;
  const key = cacheKey(source, theme);
  let pending = pendingRenders.get(key);
  if (!pending) {
    pending = enqueueRender(async () => {
      const { mermaid, DOMPurify } = await loadModules();
      mermaid.initialize(configForTheme(theme));
      const { svg } = await mermaid.render(`cc-remote-cached-${++nextRenderId}`, source);
      const safe = sanitizeSvgMarkup(svg, DOMPurify, { removeRootLayout: true });
      rememberDiagram(key, safe);
      return safe;
    });
    pendingRenders.set(key, pending);
    const complete = () => pendingRenders.delete(key);
    void pending.then(complete, complete);
  }
  return scopedDiagram(await pending, id);
}
