import { useEffect, useMemo } from "react";
import type { Turn } from "../domain/conversation";
import { viewerPageCandidates } from "../viewer-page-candidates";
import { useViewerPages } from "../viewer-pages-context";
import { Icon } from "../icons";
import "./remote-viewer.css";

export function PagePreviewLinks({ turn, sid }: { turn: Turn; sid?: string | null }) {
  const context = useViewerPages();
  const candidates = useMemo(() => context?.scope.sid === sid ? viewerPageCandidates(turn) : [],
    [context?.scope.sid, sid, turn]);
  const candidateKey = JSON.stringify(candidates);
  const discover = context && context.scope.sid === sid ? context.discover : undefined;
  const turnId = turn.historyTurnId ?? turn.clientMsgId ?? turn.id;
  useEffect(() => { discover?.(JSON.parse(candidateKey), turnId, turn.done); }, [discover, candidateKey, turnId, turn.done]);
  if (!context || context.scope.sid !== sid) return null;
  const ids = [turn.id, turn.historyTurnId, turn.clientMsgId, turn.forkPointId];
  const pages = context.pages.filter((p) => p.available && (p.references.some((r) => candidates.includes(r))
    || p.turn_ids.some((id) => ids.includes(id))));
  if (!pages.length) return null;
  const label = <><Icon name="globe" size={13} /><span>查看页面</span></>;
  return <div className="page-preview-links" aria-label="本轮页面">
    {pages.length === 1 ? <button className="page-preview-trigger" type="button"
      onClick={() => context.open(pages[0])} title={`${pages[0].label} · ${pages[0].entry}`}>
      {label}
    </button> : <details className="page-preview-menu">
      <summary className="page-preview-trigger">{label}<span>· {pages.length}</span></summary>
      <div className="page-preview-options">
        {pages.map((page) => <button type="button" key={page.id} title={page.entry}
          onClick={(event) => {
            event.currentTarget.closest("details")?.removeAttribute("open");
            context.open(page);
          }}><span>{page.label}</span><small>{page.entry}</small></button>)}
      </div>
    </details>}
  </div>;
}
