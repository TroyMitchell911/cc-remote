import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import { getLinkMetadata, subscribeLinkIcon, type LinkMetadata } from "../link-preview";
import { iconUsesThemeColor } from "../site-icon";
import type { useLinkPopover } from "../use-link-popover";

function SiteIcon({ src }: { src?: string }) {
  const [failedSrc, setFailedSrc] = useState<string>();
  const [monochromeSrc, setMonochromeSrc] = useState<string>();
  if (!src || failedSrc === src) return <Icon name="globe" size={16} />;
  if (monochromeSrc === src) return <span aria-hidden="true" className="message-site-icon is-monochrome"
    style={{ maskImage: `url("${src}")`, WebkitMaskImage: `url("${src}")` }} />;
  return <img className="message-site-icon" src={src} alt="" width="16" height="16"
    onLoad={(event) => { if (iconUsesThemeColor(event.currentTarget)) setMonochromeSrc(src); }}
    onError={() => setFailedSrc(src)} />;
}

export default function PreviewWebLink({ href, url, children, title, id, pop }: {
  href: string; url: URL; children: ReactNode; title?: string; id?: string;
  pop: ReturnType<typeof useLinkPopover>;
}) {
  const [icon, setIcon] = useState<string>();
  const [metadata, setMetadata] = useState<LinkMetadata | null>(null);
  const focusCard = useRef(false);
  const openCard = (focus: boolean) => {
    focusCard.current = focus && !pop.shown;
    if (focus && pop.shown) pop.panel.current?.querySelector<HTMLAnchorElement>("a")?.focus({ preventScroll: true });
    pop.open(true);
  };
  useLayoutEffect(() => {
    if (pop.shown && focusCard.current) {
      focusCard.current = false;
      pop.panel.current?.querySelector<HTMLAnchorElement>("a")?.focus({ preventScroll: true });
    }
  }, [pop.shown, pop.panel]);
  useEffect(() => {
    let alive = true;
    let unsubscribe = () => {};
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      unsubscribe = subscribeLinkIcon(href, (value) => { if (alive) setIcon(value); });
      void getLinkMetadata(href, "icon");
    });
    if (pop.anchor.current) observer.observe(pop.anchor.current);
    return () => { alive = false; observer.disconnect(); unsubscribe(); };
  }, [href, pop.anchor]);
  useEffect(() => {
    if (!pop.shown) return;
    let alive = true;
    // Reopening consults the bounded cache again. Holding component state
    // forever otherwise defeats expiry of failed/partial image downloads.
    void getLinkMetadata(href, "page").then((data) => { if (alive) setMetadata(data); });
    return () => { alive = false; };
  }, [href, pop.shown]);
  return <>
      <button type="button" className="message-link-icon" aria-label={`预览 ${url.hostname}`}
        aria-expanded={pop.shown} aria-controls={pop.shown ? pop.id : undefined}
        onClick={(event) => openCard(event.detail === 0)}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown") return;
          event.preventDefault(); openCard(true);
        }}><SiteIcon src={metadata?.icon || icon} /></button>
      <a href={href} id={id} target="_blank" rel="noopener noreferrer">{children}</a>
    {pop.shown && createPortal(<div ref={pop.panel} id={pop.id} role="dialog"
      aria-label={`网页预览 ${url.hostname}`} className="message-web-preview" style={pop.position}
      onPointerEnter={pop.keep} onPointerLeave={pop.leave} onFocus={pop.keep} onBlur={pop.leave}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        event.stopPropagation();
        const items = [...event.currentTarget.querySelectorAll<HTMLElement>("a,button")];
        const edge = event.shiftKey ? items[0] : items.at(-1);
        if (event.target === edge) {
          event.preventDefault();
          (event.shiftKey ? items.at(-1) : items[0])?.focus();
        }
      }}>
      <div className="message-web-preview-header"><SiteIcon src={metadata?.icon || icon} />
        <span>{url.hostname}</span>
        <a href={href} target="_blank" rel="noopener noreferrer">打开 ↗</a>
        <button type="button" onClick={() => {
          pop.anchor.current?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
          pop.close();
        }} aria-label="关闭网页预览"><Icon name="close" size={16} /></button>
      </div>
      {metadata?.image && <img className="message-web-preview-cover" src={metadata.image} alt=""
        onError={() => setMetadata((value) => value ? { ...value, image: undefined } : value)} />}
      <div className="message-web-preview-body">
        <div className="message-web-preview-title">{metadata?.title || title || children}</div>
        <p>{metadata?.description || (metadata ? "打开网页查看完整内容" : "正在加载预览…")}</p>
      </div>
    </div>, document.body)}
  </>;
}
