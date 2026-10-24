import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import "./DshFeatures.css";

export function DshChildControl({ parent, target, api }: { parent: string; target: string; api: DshApiClient }) {
  const [child, setChild] = useState<DshItem | null>(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    api.read(parent, "subagents", { signal: abort.signal }).then(result => {
      if (!abort.signal.aborted) setChild(result.error ? null : result.items.find(item => item.sid === target) ?? null);
    }).catch(() => { if (!abort.signal.aborted) setChild(null); });
    const timer = setTimeout(() => setRevision(value => value + 1), 5000);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [api, parent, target, revision]);
  const act = async (action: "queue" | "steer" | "stop") => {
    setBusy(true); setNotice("");
    try {
      const result = await api.act(parent, target, action, prompt);
      setNotice(result.text);
      if (result.status === "success" && action !== "stop") setPrompt("");
      setRevision(value => value + 1);
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  if (!child || child.mode !== "continuable" || !child.controllable) return null;
  return <form className="dsh-child-composer" onSubmit={event => { event.preventDefault(); void act("queue"); }}>
    <textarea aria-label="子代理消息" placeholder="发消息给这个子代理…" value={prompt} onChange={event => setPrompt(event.target.value)} maxLength={65536} />
    <div><button type="submit" disabled={busy || !prompt.trim()}>排队发送</button><button type="button" disabled={busy || !prompt.trim()} onClick={() => void act("steer")}>引导</button><button type="button" disabled={busy || child.state !== "running"} onClick={() => void act("stop")}>停止子代理</button></div>
    {notice && <p role="status">{notice}</p>}
  </form>;
}
