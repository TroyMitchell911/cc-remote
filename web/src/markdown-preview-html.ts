import type { Element, Nodes, Root } from "hast";
import type { VFile } from "vfile";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema, type Options } from "rehype-sanitize";

const ID_PREFIX = "cc-preview-";
const schema: Options = {
  ...defaultSchema,
  clobberPrefix: ID_PREFIX,
  // Do not let <source srcset> bypass PreviewImage's URL and local-file routing.
  tagNames: defaultSchema.tagNames?.filter((tag) => tag !== "source"),
  strip: [
    "script", "style", "iframe", "object", "embed", "form", "textarea",
    "select", "button", "svg", "math", "template", "link", "meta", "base",
  ],
  attributes: {
    ...defaultSchema.attributes,
    "*": ["id", "title", "lang", ["dir", "ltr", "rtl", "auto"],
      ["align", "left", "center", "right", "justify"]],
    a: [...defaultSchema.attributes!.a, "name"],
    img: ["src", "alt", "width", "height"],
    details: ["open"],
    // Sanitize BEFORE trusted KaTeX output; preserve its input markers, not
    // arbitrary style/class names supplied by the document.
    code: [["className", /^language-[\w-]+$/, "math-inline", "math-display"]],
    input: [...defaultSchema.attributes!.input, "checked"],
    ol: [...defaultSchema.attributes!.ol, "start"],
    li: [...defaultSchema.attributes!.li, "value"],
    td: ["colSpan", "rowSpan"],
    th: ["colSpan", "rowSpan"],
  },
};

/** File previews only: parse README HTML, then apply a restrictive GitHub-style
 * sanitizer. Chat messages keep their separate details-only HTML contract.
 * No DOM insertion, scripts, styles, event handlers or raw local image URLs.
 */
export function rehypePreviewHtml() {
  const parse = rehypeRaw();
  const sanitize = rehypeSanitize(schema);
  return (tree: Root, file: VFile): Root => {
    const safe = sanitize(parse(tree, file));
    const pending: Nodes[] = [safe];
    const anchors = new Map<string, string>();
    const links: Element[] = [];
    while (pending.length) {
      const node = pending.pop()!;
      if (node.type === "element") {
        if (node.tagName === "details") node.properties.className = ["message-disclosure"];
        const destination = node.properties.id || node.properties.name;
        for (const name of ["id", "name"]) {
          const value = node.properties[name];
          if (typeof value === "string" && value.startsWith(ID_PREFIX)
              && typeof destination === "string") {
            anchors.set(value.slice(ID_PREFIX.length), destination);
          }
        }
        if (node.tagName === "a") {
          // Legacy <a name> anchors become scoped ids; aliases still resolve
          // when a document supplies both id and name on the same anchor.
          if (typeof destination === "string") node.properties.id = destination;
          delete node.properties.name;
          links.push(node);
        }
      }
      if ("children" in node) {
        for (const child of node.children) pending.push(child);
      }
    }
    // Keep in-document links/footnotes working after DOM-clobbering prefixes.
    for (const link of links) {
      const href = link.properties.href;
      if (typeof href !== "string" || !href.startsWith("#")) continue;
      try {
        const target = decodeURIComponent(href.slice(1));
        if (anchors.has(target)) link.properties.href = `#${anchors.get(target)}`;
      } catch { /* Invalid fragment escapes stay inert link text/anchors. */ }
    }
    return safe;
  };
}

/** HTML dimensions are hints, never arbitrary CSS or unbounded layout values. */
export function previewImageDimension(value: unknown): string | number | undefined {
  const text = String(value ?? "");
  if (/^(?:[1-9]\d?|100)%$/.test(text)) return text;
  const pixels = Number(text);
  return /^\d{1,4}$/.test(text) && pixels > 0 && pixels <= 4096 ? pixels : undefined;
}
