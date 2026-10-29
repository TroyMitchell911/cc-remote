import { Fragment, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLinkPopover } from "../use-link-popover";

/** A portal keeps path hints out of the sidebar's layout and scroll extent. */
export function SessionPathTitle({ path, children }: {
  path: string;
  children: ReactNode;
}) {
  const pop = useLinkPopover(500);
  return <>
    <span ref={pop.anchor} className="scard-title"
      aria-describedby={pop.shown ? pop.id : undefined}
      onPointerEnter={pop.enter} onPointerLeave={pop.leave}
      onPointerDown={pop.close}>
      {children}
    </span>
    {pop.shown && createPortal(<div ref={pop.panel} id={pop.id}
      role="tooltip" className="scard-path-tip" style={pop.position}>
      {path.split(/(?<=\/)/).map((part, index) =>
        <Fragment key={index}>{part}<wbr /></Fragment>)}
    </div>, document.body)}
  </>;
}
