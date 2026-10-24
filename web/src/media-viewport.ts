/** Expand the chat/artifact scrollport, not the outer browser viewport.
 * A viewport-root observer is clipped by nested scrollers before rootMargin
 * is applied, so it cannot warm media just outside a chat's visible area. */
export function mediaViewportOptions(node: HTMLElement): IntersectionObserverInit {
  let root = node.parentElement;
  while (root && root !== document.body && root !== document.documentElement) {
    if (/^(auto|scroll|overlay)$/.test(getComputedStyle(root).overflowY)) break;
    root = root.parentElement;
  }
  if (root === document.body || root === document.documentElement) root = null;
  const height = root?.clientHeight || window.innerHeight;
  return { root, rootMargin: `${Math.min(1600, Math.max(600, height * 2))}px 0px` };
}
