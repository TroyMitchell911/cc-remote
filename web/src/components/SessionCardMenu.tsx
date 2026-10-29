import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

/** Keep session actions outside the sidebar's scrolling/clipping ancestors. */
export function SessionCardMenu({ anchor, onClose, children }: {
  anchor: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const menuRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const trigger = anchor.current;
    const menu = menuRef.current;
    const sidebar = trigger?.closest<HTMLElement>(".sessions");
    const scroll = trigger?.closest<HTMLElement>(".s-scroll");
    const footer = sidebar?.querySelector<HTMLElement>(".s-foot");
    if (!trigger || !menu || !sidebar || !scroll || !footer) return;
    const card = trigger.closest<HTMLElement>(".scard");
    const viewport = window.visualViewport;
    const position = () => {
      const list = scroll.getBoundingClientRect();
      // The card scales visually during a hold. Anchor to its layout coordinates
      // so resize/scroll callbacks never sample an intermediate animation frame.
      // The scroller's rect still follows the drawer and visual viewport.
      const reference = card?.classList.contains("lifting") ? card : trigger;
      let leftOffset = 0, topOffset = 0;
      let parent: HTMLElement | null = reference;
      while (parent && parent !== scroll) {
        leftOffset += parent.offsetLeft;
        topOffset += parent.offsetTop;
        parent = parent.offsetParent as HTMLElement | null;
        if (parent) {
          leftOffset += parent.clientLeft;
          topOffset += parent.clientTop;
        }
      }
      if (parent !== scroll) { onClose(); return; }
      const button = {
        top: list.top + topOffset - scroll.scrollTop,
        bottom: list.top + topOffset - scroll.scrollTop + reference.offsetHeight,
        right: list.left + leftOffset - scroll.scrollLeft + reference.offsetWidth,
      };
      if (!trigger.isConnected || button.bottom <= list.top || button.top >= list.bottom) {
        onClose();
        return;
      }
      const panel = sidebar.getBoundingClientRect();
      const viewLeft = viewport?.offsetLeft ?? 0;
      const viewTop = viewport?.offsetTop ?? 0;
      const left = Math.max(panel.left, viewLeft) + 8;
      const right = Math.min(panel.right, viewLeft + (viewport?.width ?? window.innerWidth)) - 8;
      const top = Math.max(panel.top, viewTop) + 8;
      const bottom = Math.min(footer.getBoundingClientRect().top,
        viewTop + (viewport?.height ?? window.innerHeight)) - 8;
      const width = Math.max(0, right - left);
      menu.style.minWidth = `${Math.min(152, width)}px`;
      menu.style.maxWidth = `${width}px`;
      const height = menu.scrollHeight + menu.offsetHeight - menu.clientHeight;
      const above = Math.max(0, Math.min(button.top - 6, bottom) - top);
      const below = Math.max(0, bottom - Math.max(button.bottom + 6, top));
      const upward = height > below && above > below;
      const available = upward ? above : below;
      menu.style.maxHeight = `${available}px`;
      const box = menu.getBoundingClientRect();
      menu.style.left = `${Math.max(left, Math.min(button.right - box.width, right - box.width))}px`;
      menu.style.top = `${Math.max(top, Math.min(
        upward ? button.top - 6 - box.height : button.bottom + 6, bottom - box.height,
      ))}px`;
      menu.dataset.placement = upward ? "above" : "below";
      menu.style.visibility = "visible";
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
      trigger.focus({ preventScroll: true });
    };
    position();
    if (document.activeElement === trigger && trigger.matches(":focus-visible")) {
      menu.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
    }
    let frame = 0;
    const schedulePosition = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = 0; position(); });
    };
    const sidebarTransition = (event: TransitionEvent) => {
      // A child's hover/highlight transition must not move the menu again.
      if (event.target === sidebar && event.propertyName === "transform") schedulePosition();
    };
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && menu.contains(event.target)) return;
      schedulePosition();
    };
    const observer = new ResizeObserver(schedulePosition);
    for (const element of [menu, trigger, card, sidebar, scroll, footer]) {
      if (element) observer.observe(element);
    }
    window.addEventListener("resize", schedulePosition);
    window.addEventListener("scroll", onScroll, true);
    viewport?.addEventListener("resize", schedulePosition);
    viewport?.addEventListener("scroll", schedulePosition);
    sidebar.addEventListener("transitionend", sidebarTransition);
    document.addEventListener("keydown", escape, true);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedulePosition);
      window.removeEventListener("scroll", onScroll, true);
      viewport?.removeEventListener("resize", schedulePosition);
      viewport?.removeEventListener("scroll", schedulePosition);
      sidebar.removeEventListener("transitionend", sidebarTransition);
      document.removeEventListener("keydown", escape, true);
    };
  }, [anchor, onClose]);

  return typeof document === "undefined" ? null : createPortal(
    <div ref={menuRef} className="card-menu" role="group" aria-label="会话操作"
      onClick={event => event.stopPropagation()}
      onTouchStart={event => event.stopPropagation()}
      onKeyDown={event => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }}>
      {children}
    </div>, document.body,
  );
}
