import { useCallback, useEffect, useRef, useState } from "react";

import {
  acceptAgentDetail,
  emptyAgentRun,
  type AgentDetailPanelState,
} from "../agent-detail";
import type { AgentDetail } from "../protocol";
import type { RelayWs } from "../ws";
import { uuid } from "../util";
import { AgentDetailPanel } from "./AgentDetailPanel";

export interface AgentDetailSelection {
  sid: string;
  revision: string;
  runId: string;
  title: string;
}

export function AgentDetailController({ selection, ws, onListen, onClose,
  onOpenFile }: {
  selection: AgentDetailSelection;
  ws: RelayWs | null;
  onListen: (listener: ((message: AgentDetail) => void) | null) => void;
  onClose: () => void;
  onOpenFile?: (path: string, line?: number) => void;
}) {
  const [panel, setPanel] = useState<AgentDetailPanelState>(() => ({
    sid: selection.sid,
    revision: selection.revision,
    stack: [selection.runId],
    runs: { [selection.runId]: emptyAgentRun(
      selection.runId, selection.title) },
  }));
  const panelRef = useRef(panel);
  panelRef.current = panel;

  const request = useCallback((runId: string, title = "协作代理",
    before?: string | null) => {
    const current = panelRef.current;
    const run = current.runs[runId];
    const requestId = uuid();
    if (!ws?.sendGetAgentDetail(
      selection.sid, runId, selection.revision,
      run?.detailRevision, before, 192, requestId,
    )) return false;
    setPanel((value) => {
      const currentRun = value.runs[runId] ?? emptyAgentRun(runId, title);
      return { ...value, runs: { ...value.runs, [runId]: {
        ...currentRun, title: title || currentRun.title,
        loading: true, error: null, requestId,
      } } };
    });
    return true;
  }, [selection.revision, selection.sid, ws]);

  const receive = useCallback((message: AgentDetail) => {
    const current = panelRef.current;
    const run = current.runs[message.run_id];
    if (!run || message.session_id !== current.sid
        || message.revision !== current.revision) return;
    if (!message.live && (!message.request_id
        || message.request_id !== run.requestId)) return;
    setPanel((value) => {
      const currentRun = value.runs[message.run_id];
      if (!currentRun) return value;
      if (!message.live && message.request_id !== currentRun.requestId) {
        return value;
      }
      return { ...value, runs: { ...value.runs,
        [message.run_id]: acceptAgentDetail(currentRun, message) } };
    });
  }, []);

  useEffect(() => {
    onListen(receive);
    return () => onListen(null);
  }, [onListen, receive]);
  useEffect(() => {
    request(selection.runId, selection.title);
  }, [request, selection.runId, selection.title]);

  const open = (runId: string, title?: string) => {
    if (!request(runId, title)) return;
    setPanel((value) => ({ ...value,
      stack: value.stack.at(-1) === runId
        ? value.stack : [...value.stack, runId] }));
  };
  const activeId = panel.stack.at(-1);
  const run = activeId ? panel.runs[activeId] : null;
  if (!run) return null;
  return <AgentDetailPanel run={run} canGoBack={panel.stack.length > 1}
    onBack={() => setPanel((value) => ({
      ...value, stack: value.stack.slice(0, -1),
    }))}
    onClose={onClose}
    onRetry={() => request(run.runId, run.title)}
    onLoadEarlier={() => {
      if (run.oldestCursor && !run.loading) {
        request(run.runId, run.title, run.oldestCursor);
      }
    }}
    onOpenAgent={open}
    onOpenFile={onOpenFile} />;
}
