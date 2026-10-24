import { useMemo, useState } from "react";
import type { ServerEvent } from "../protocol";
import { projectAgentEvents } from "../agent-detail";
import { finalTextBlocks, presentableProcessBlocks } from "../process-blocks";
import { MessageBlock } from "./MessageBlock";
import { ProcessTimeline } from "./ProcessTimeline";

export function DshAgentConversation({ events, done, onOpenAgent, onOpenFile }: {
  events: ServerEvent[]; done: boolean;
  onOpenAgent: (id: string, title?: string) => void; onOpenFile?: (path: string, line?: number) => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const turns = useMemo(() => {
    const rows: { id: string; prompt?: string; events: ServerEvent[] }[] = [];
    let current = { id: "earlier", events: [] as ServerEvent[], prompt: undefined as string | undefined };
    rows.push(current);
    for (const event of events) {
      if (event.type === "user_msg" || event.type === "turn_steered") {
        current = { id: event.msg_id, prompt: event.prompt, events: [] }; rows.push(current);
      } else current.events.push(event);
    }
    return rows.map(row => ({ ...row, blocks: projectAgentEvents(row.events) }));
  }, [events]);
  return <>{turns.map((turn, index) => {
    const finished = done || index < turns.length - 1;
    const process = presentableProcessBlocks(turn.blocks, "dsh");
    return <section key={turn.id} className="dsh-agent-turn">
      {turn.prompt && <div className="dsh-agent-prompt">{turn.prompt}</div>}
      {process.length > 0 && <ProcessTimeline blocks={turn.blocks} engine="dsh" done={finished} active={!finished}
        openOverride={expanded[turn.id] ?? !finished} onOpenChange={value => setExpanded(previous => ({ ...previous, [turn.id]: value }))}
        onOpenAgent={onOpenAgent} onOpenFile={onOpenFile} />}
      {finalTextBlocks(turn.blocks).map(block => <MessageBlock key={block.message_id} text={block.text} done={block.done} onOpenFile={onOpenFile} />)}
    </section>;
  })}</>;
}
