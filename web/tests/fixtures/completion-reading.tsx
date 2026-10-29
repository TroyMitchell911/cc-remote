import { useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import type { Turn } from "../../src/reducer";

export function CompletionReadingFixture() {
  const [done, setDone] = useState(false);
  const [extra, setExtra] = useState(0);
  const shortProcess = new URLSearchParams(location.search).has("short-process");
  const turns: Turn[] = [{
    id: "previous", prompt: "Earlier question", done: true,
    blocks: [{ kind: "text", message_id: "previous-answer", done: true,
      text: Array.from({ length: 20 }, (_, i) => `Earlier answer paragraph ${i}.`).join("\n\n") }],
  }, {
    id: "answering", prompt: "Explain the implementation", done,
    blocks: [
      { kind: "text", message_id: "thinking", channel: "thinking", done: true,
        text: Array.from({ length: shortProcess ? 1 : 30 }, (_, i) => `Thinking paragraph ${i}. Checking the implementation before answering.`).join("\n\n") },
      { kind: "text", message_id: "answer", channel: "final", done,
        text: Array.from({ length: 20 }, (_, i) => `Answer paragraph ${i}. This is the explanation the user is reading.`).join("\n\n")
          + "\n\nFinal additional paragraph.\n\n".repeat(extra * 8) },
    ],
  }];
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh" }}>
    <header style={{ height: 48, flex: "none" }}>Reading a streaming answer</header>
    <ChatView sid="completion-reading" engine="claude" turns={turns}
      historyRevision="stable" activeTurnId={done ? null : "answering"} />
    <footer style={{ height: 48, flex: "none" }}>
      <button data-testid="append-answer" onClick={() => setExtra(value => value + 1)}>Append answer</button>
      <button data-testid="complete-answer" onClick={() => setDone(true)}>Complete answer</button>
    </footer>
  </main>;
}
