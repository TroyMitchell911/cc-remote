import { useRef, useState } from "react";
import { RollbackSheet } from "../../src/components/RollbackSheet";
import { useClaudeRewind } from "../../src/use-claude-rewind";
import { ComposerDraftStore } from "../../src/composer-drafts";
import type { RelayWs } from "../../src/ws";
import { PROTOCOL_VERSION, type RestoreMode, type ServerEvent } from "../../src/protocol";

export function RewindFixture() {
  document.documentElement.dataset.theme = "dark";
  document.documentElement.dataset.engine = "claude";
  const [scope, setScope] = useState("session-a");
  const [input, setInput] = useState("");
  const [sent, setSent] = useState(0);
  const serial = useRef(0);
  const event = useRef((_: ServerEvent) => {});
  const receipt = useRef<ServerEvent | null>(null);
  const drafts = useRef(new ComposerDraftStore());
  const prompt = "请恢复到实现这一步之前，并检查计划中的变更。\n".repeat(120) + "x".repeat(3000);
  const noFiles = new URLSearchParams(location.search).has("no-files");
  const ws = useRef({
    sendGetRewindPreview(sessionId: string, checkpointId: string) {
      const id = `preview-${++serial.current}`;
      setTimeout(() => event.current({ v: PROTOCOL_VERSION, ts: 1, type: "rewind_preview",
        request_id: id, session_id: sessionId, checkpoint_id: checkpointId,
        available: true, files_available: !noFiles, latest_user_message_id: "latest",
        num_turns: 3, prompt }), 10);
      return id;
    },
    sendClaudeRewind(sessionId: string, _target: string, _latest: string, _count: number, restore: RestoreMode) {
      const id = `rewind-${++serial.current}`;
      setSent(n => n + 1);
      receipt.current = { v: PROTOCOL_VERSION, ts: 2, type: "rollback_result", request_id: id,
        session_id: sessionId, engine: "claude", restore, conversation: restore === "files" ? "skipped" : "succeeded",
        files: restore === "conversation" ? "skipped" : "succeeded", restored_turns: 3,
        conflicts: [], prefill_text: "原消息" };
      return id;
    },
  } as unknown as RelayWs);
  const rewind = useClaudeRewind({ scope, sid: scope, disabledReason: null, ws,
    drafts: drafts.current, onPrefill: setInput });
  event.current = value => rewind.onEventRef.current(value);
  const target = { checkpointId: "point-a", prompt, ts: 1793000000000 };
  return <main style={{ padding: 20 }}>
    <h1>Code · Claude</h1>
    <button onClick={() => rewind.open(target)}>打开回退</button>
    <button onClick={() => {
      const next = scope === "session-a" ? "session-b" : "session-a";
      setScope(next); setInput(drafts.current.get(next).input);
    }}>切换会话</button>
    <button onClick={() => receipt.current && event.current(receipt.current)}>返回回退结果</button>
    <button onClick={() => { setInput("保留我的新草稿"); drafts.current.set(scope,
      { input: "保留我的新草稿", images: [], files: [], pastes: [] }); }}>写入新草稿</button>
    <textarea aria-label="草稿" value={input} readOnly />
    <output data-testid="sent">{sent}</output>
    {rewind.view && <RollbackSheet {...rewind.view} choices={[target]} disabledReason={null}
      onClose={rewind.close} onChoose={rewind.open} onConfirm={rewind.confirm} />}
  </main>;
}
