import type { Turn } from "../domain/conversation";
import { Icon } from "../icons";
import "./session-messages.css";

export default function SessionMessageLinks({ turn, resolve, onOpen }: {
  turn: Turn;
  resolve?: (nativeId: string) => { title: string; available: boolean };
  onOpen?: (nativeId: string) => void;
}) {
  const link = (id: string, label: string, key: string) => {
    const target = resolve?.(id);
    const enabled = !!onOpen && !!target?.available;
    return <button key={key} type="button"
      className="session-message-source"
      disabled={!enabled} title={enabled ? "进入会话" : "会话不可用或不在当前可见范围中"}
      onClick={() => onOpen?.(id)}>
      <Icon name="message" size={13} />
      <span>{label} {target?.title ?? id.slice(0, 8)}</span>
      {enabled && <span aria-hidden="true">↗</span>}
    </button>;
  };
  return turn.sourceThreadId
    ? link(turn.sourceThreadId, "来自会话 ·", "source") : null;
}
