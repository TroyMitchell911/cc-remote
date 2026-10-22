import type { Element, Root } from "hast";

const BARE_BREAK = /^<br[\t\n\r ]*\/?>$/i;
type Parent = Root | Element;

function removeLeadingBreaks(parent: Parent): boolean {
  let removed = false;
  for (let index = 0; index < parent.children.length;) {
    const child = parent.children[index];
    if (child.type === "text" && !child.value.trim()) {
      index++;
    } else if (child.type === "element" && child.tagName === "br") {
      parent.children.splice(index, 1);
      removed = true;
    } else {
      break;
    }
  }
  return removed;
}

/** A bare HTML break is common in model-generated GFM tables. Convert only
 * that exact raw node; code, escaped examples and other HTML remain literal.
 * Ignore an initial spacer before the answer, without trimming later breaks.
 */
export function rehypeMessageBreaks() {
  return (tree: Root): void => {
    const pending: Parent[] = [tree];
    while (pending.length) {
      const parent = pending.pop()!;
      parent.children.forEach((child, index) => {
        if (child.type === "raw" && BARE_BREAK.test(child.value)) {
          parent.children[index] = {
            type: "element", tagName: "br", properties: {}, children: [],
            position: child.position,
          };
        } else if (child.type === "element") {
          pending.push(child);
        }
      });
    }
    removeLeadingBreaks(tree);
    for (let index = 0; index < tree.children.length; index++) {
      const child = tree.children[index];
      if (child.type === "text" && !child.value.trim()) continue;
      if (child.type !== "element" || child.tagName !== "p") break;
      if (!removeLeadingBreaks(child)) break;
      if (child.children.some(node => node.type !== "text" || node.value.trim())) break;
      tree.children.splice(index--, 1);
    }
  };
}
