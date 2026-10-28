import { useEffect, useRef, useState, type RefObject } from "react";
import type { RelayWs } from "./ws";
import type { ComposerDraftStore } from "./composer-drafts";
import type { RestoreMode, RewindPreview, RollbackResult, ServerEvent } from "./protocol";
import type { RollbackTarget } from "./components/RollbackSheet";
import { prefillRewindDraft } from "./claude-rewind";

interface View {
  scope: string;
  target: RollbackTarget | null;
  preview: RewindPreview | null;
  previewId: string | null;
  pending: boolean;
  result: RollbackResult | null;
  error: string | null;
  prefillSkipped: boolean;
}

export function useClaudeRewind({ scope, sid, disabledReason, ws, drafts, onPrefill }: {
  scope: string;
  sid: string | null;
  disabledReason: string | null;
  ws: RefObject<RelayWs | null>;
  drafts: ComposerDraftStore;
  onPrefill: (prompt: string) => void;
}) {
  const [view, setView] = useState<View | null>(null);
  const viewRef = useRef(view);
  const current = useRef({ scope, sid, disabledReason, onPrefill });
  current.current = { scope, sid, disabledReason, onPrefill };
  const pending = useRef<{ id: string; scope: string; sid: string } | null>(null);
  const update = (value: View | null) => { viewRef.current = value; setView(value); };
  useEffect(() => {
    if (!view?.previewId || view.preview || view.error) return;
    const id = view.previewId;
    const timer = window.setTimeout(() => {
      if (viewRef.current?.previewId === id) update({ ...viewRef.current, error: "检查回退点超时，请关闭后重试" });
    }, 30_000);
    return () => window.clearTimeout(timer);
  }, [view?.previewId, view?.preview, view?.error]);

  const onEventRef = useRef((_: ServerEvent) => {});
  onEventRef.current = (event) => {
    const active = viewRef.current;
    if (event.type === "rewind_preview" && active && active.previewId === event.request_id) {
      update({ ...active, preview: event, error: null });
    }
    if (event.type !== "rollback_result" || !pending.current
        || event.request_id !== pending.current.id || event.session_id !== pending.current.sid) return;
    const owner = pending.current;
    pending.current = null;
    let prefillSkipped = false;
    if (event.conversation === "succeeded" && event.prefill_text) {
      const applied = prefillRewindDraft(drafts, owner.scope, event.prefill_text);
      prefillSkipped = !applied;
      if (applied && current.current.scope === owner.scope) current.current.onPrefill(event.prefill_text);
    }
    if (active?.scope === owner.scope) {
      const succeeded = (event.restore === "files" || event.conversation === "succeeded")
        && (event.restore === "conversation" || event.files === "succeeded");
      update(succeeded ? null : { ...active, pending: false, result: event, prefillSkipped });
    }
  };
  const onCompletedRef = useRef((_: string) => {});
  onCompletedRef.current = (id) => {
    const active = viewRef.current;
    if (pending.current?.id === id) {
      pending.current = null;
      if (active) update({ ...active, pending: false, error: "回退未完成，请查看会话中的错误提示，再重新选择回退点" });
    } else if (active?.previewId === id && !active.preview) {
      update({ ...active, error: "未能检查回退点，请关闭后重试" });
    }
  };
  return {
    view: view?.scope === scope ? view : null,
    onEventRef, onCompletedRef,
    open(target: RollbackTarget | null = null) {
      if (!sid || pending.current) return;
      const previewId = target && !disabledReason
        ? ws.current?.sendGetRewindPreview(sid, target.checkpointId) ?? null : null;
      update({ scope, target, previewId, preview: null, pending: false, result: null,
        error: target && !previewId ? disabledReason || "暂时无法连接，请稍后重试" : null,
        prefillSkipped: false });
    },
    close() { if (!pending.current) update(null); },
    confirm(mode: RestoreMode) {
      const active = viewRef.current;
      const preview = active?.preview;
      if (!sid || current.current.disabledReason || active?.scope !== scope || active.error
          || pending.current || active.result || !preview?.available
          || !preview.latest_user_message_id || (mode !== "conversation" && !preview.files_available)) return;
      const id = ws.current?.sendClaudeRewind(sid, preview.checkpoint_id,
        preview.latest_user_message_id, preview.num_turns, mode);
      if (!id) { update({ ...active, error: "回退尚未发送，请等待连接恢复后重试" }); return; }
      pending.current = { id, scope, sid };
      update({ ...active, pending: true });
    },
  };
}
