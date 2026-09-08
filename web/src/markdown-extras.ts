import type { Nodes, Root } from "hast";
import type { VFile } from "vfile";
import rehypeRaw from "rehype-raw";

const DISCLOSURE_TAG = /(<\/?(?:details|summary)\s*>|<details\s+open\s*>)/gi;
const SAFE_TAG = /^<\/?(?:details|summary)\s*>$|^<details\s+open\s*>$/i;

/** Recognize only native disclosures, not arbitrary model-provided HTML.
 * Everything else remains visible text as in react-markdown's default mode.
 * The existing HTML parser owns nesting/streaming repair. Boolean `open` is the
 * only allowed attribute: raw URLs, event handlers and styles stay inert.
 */
export function rehypeSafeDetails() {
  const parse = rehypeRaw({ tagfilter: true });
  return (tree: Root, file: VFile): Root => {
    const pending: Nodes[] = [tree];
    while (pending.length) {
      const node = pending.pop()!;
      if (node.type === "raw") {
        node.value = node.value.split(DISCLOSURE_TAG).map((part) =>
          SAFE_TAG.test(part) ? part.toLowerCase().replace(
            /^<details(?=[\s>])/, '<details class="message-disclosure"',
          )
            : part.replace(/</g, "&lt;").replace(/>/g, "&gt;"),
        ).join("");
      } else if ("children" in node) {
        for (const child of node.children) pending.push(child);
      }
    }
    return parse(tree, file);
  };
}
