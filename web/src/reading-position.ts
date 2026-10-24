/** A reading block's content coordinate, independent of native scroll motion.
 * Keep this separate from follow-output and explicit history/detail anchors. */
export interface ReadingPosition {
  scope: string;
  node: HTMLElement;
  /** Block top in scroll-content coordinates. */
  top: number;
}

// The outer media node survives pending -> ready. Anchoring its source <pre>
// loses the reading position when Mermaid replaces that node with an SVG.
const READING_BLOCKS = ".prose :is(p,li,pre,table,h1,h2,h3,h4,h5,h6),.mermaid-block,.generated-output,.message-image-frame,.user-bubble,.turn-process-head";

export function captureReadingPosition(viewport: HTMLElement, scope: string): ReadingPosition | null {
  const top = viewport.getBoundingClientRect().top;
  const bottom = top + viewport.clientHeight;
  const firstVisible = (nodes: NodeListOf<HTMLElement>) => {
    for (const node of nodes) {
      const rect = node.getBoundingClientRect();
      if (rect.height > 0 && rect.bottom > top && rect.top < bottom) return node;
    }
  };
  const turn = firstVisible(viewport.querySelectorAll<HTMLElement>("[data-turn-id]"));
  if (!turn) return null;
  // Only inspect blocks in a visible turn. A whole-turn anchor cannot keep
  // a paragraph in place when text earlier in that same answer reflows.
  const node = firstVisible(turn.querySelectorAll<HTMLElement>(READING_BLOCKS)) ?? turn;
  return { scope, node, top: node.getBoundingClientRect().top - top + viewport.scrollTop };
}

/** Returns only layout displacement. Adding scrollTop cancels any wheel/key
 * movement since capture, so correcting this delta cannot undo that movement. */
export function readingPositionDelta(
  viewport: HTMLElement, position: ReadingPosition,
): number | null {
  const rect = position.node.getBoundingClientRect();
  if (!viewport.contains(position.node) || !rect.height) return null;
  const contentTop = rect.top
    - viewport.getBoundingClientRect().top + viewport.scrollTop;
  const delta = contentTop - position.top;
  position.top = contentTop;
  return delta;
}

/** A semantic bookmark survives unmount; raw scrollTop alone does not survive
 * virtual-row estimates, a resized sidebar, or new output above the reader. */
export function captureReadingBookmark(viewport: HTMLElement) {
  const reading = captureReadingPosition(viewport, "");
  const turn = reading?.node.closest<HTMLElement>("[data-turn-id]");
  if (!reading || !turn?.dataset.turnId) return null;
  const top = viewport.getBoundingClientRect().top;
  return {
    turnId: turn.dataset.turnId,
    blockIndex: [...turn.querySelectorAll(READING_BLOCKS)].indexOf(reading.node),
    offset: reading.node.getBoundingClientRect().top - top,
    turnOffset: turn.getBoundingClientRect().top - top,
    scrollTop: viewport.scrollTop,
  };
}

export function bookmarkedReadingNode(turn: HTMLElement, blockIndex: number): HTMLElement {
  return turn.querySelectorAll<HTMLElement>(READING_BLOCKS)[blockIndex] ?? turn;
}
