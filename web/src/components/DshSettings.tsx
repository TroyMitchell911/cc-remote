import { useEffect, useState } from "react";
import { Icon } from "../icons";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";

type Values = Record<string, string | number | boolean | null>;
type Draft = { revision: unknown; base: Values; values: Values };
const names: Record<string, string> = {
  subagent: "子代理", "agent-loop": "工具调用", loop: "循环", "tool-bash": "命令执行",
  shell: "Shell", "web-search": "网络搜索", maxIterations: "最大循环次数", maxConcurrency: "最大并发数",
  maxSteps: "最大步数", timeout: "超时时间", timeoutMs: "超时（毫秒）", enabled: "启用",
  maxDepth: "最大层级", maxActiveSubagents: "并发子代理", maxParallelToolCalls: "并行调用上限",
};
const tips: Record<string, string> = {
  maxDepth: "1 允许主代理创建子代理；提高后可继续创建下级。0 禁止创建。",
  maxActiveSubagents: "最多同时运行的子代理数量。",
  maxParallelToolCalls: "单次回复中，最多同时执行的工具数量。",
};
function patchFor(draft: Draft): Values {
  return Object.fromEntries(Object.entries(draft.values).map(([key, value]) => [key,
    typeof draft.base[key] === "number" && String(value).trim() !== "" ? Number(value) : value,
  ]).filter(([key, value]) => value !== draft.base[String(key)]));
}

export function DshSettings({ sid, api }: { sid: string; api: DshApiClient }) {
  const [items, setItems] = useState<DshItem[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const changes = Object.entries(drafts).filter(([, draft]) => Object.keys(patchFor(draft)).length);
  const count = changes.reduce((total, [, draft]) => total + Object.keys(patchFor(draft)).length, 0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError("");
    api.read(sid, "settings", { signal: abort.signal }).then(result => {
      if (abort.signal.aborted) return;
      if (result.error) { setError(result.error); return; }
      setItems(result.items.filter(item => item.id !== "agent-preset-registry" && Object.keys(item.data?.values ?? {}).length));
    }).catch(error => { if (!abort.signal.aborted) setError(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, api, revision]);
  const save = async () => {
    if (!count || busy) return;
    setBusy(true); setNotice(""); setError("");
    let saved = 0;
    try {
      // Each namespace has its own native revision. Keep unsaved drafts and
      // stop on the first uncertain receipt; never replay an accepted write.
      for (const [ns, draft] of changes) {
        const patch = patchFor(draft);
        const result = await api.control(sid, "manage", JSON.stringify({ action: "settings", ns, revision: draft.revision, patch }));
        if (result.status !== "success") throw new Error(result.text || "设置结果尚未确认，请刷新核实");
        saved += Object.keys(patch).length;
        setDrafts(current => { const next = { ...current }; delete next[ns]; return next; });
        // The next read supplies the new revision. Do not edit against an old
        // revision even if that read fails after a successful save.
        setItems(current => current.map(item => item.id === ns ? { ...item, controllable: false, data: { ...item.data, values: { ...draft.base, ...patch } } } : item));
      }
      setNotice(`已保存 ${saved} 项修改`);
    } catch (error) {
      setNotice(`${saved ? `已保存 ${saved} 项；` : ""}${(error as Error).message}。未确认的修改已保留，不会自动重发。`);
    } finally { setBusy(false); setRevision(value => value + 1); }
  };
  return <form className="dsh-management dsh-settings" onSubmit={event => { event.preventDefault(); void save(); }}>
    <div className="dsh-settings-scope"><span>当前设备的 DSH 共用</span><button type="button" className="iconbtn" aria-label="刷新运行设置" disabled={busy || loading || !!count} onClick={() => setRevision(value => value + 1)}><Icon name="refresh" size={17} /></button></div>
    <div className="dsh-management-content">
      {error && <p role="alert" className="dsh-feature-error">{error}</p>}
      {items.map(item => {
        const values = (item.data?.values ?? {}) as Values;
        const draft = drafts[item.id];
        const constraints = (item.data?.constraints ?? {}) as Record<string, { min?: number; max?: number; step?: number }>;
        const change = (key: string, value: string | boolean) => setDrafts(current => {
          const old = current[item.id] ?? { revision: item.data?.revision, base: values, values };
          return { ...current, [item.id]: { ...old, values: { ...old.values, [key]: value } } };
        });
        return <fieldset className="dsh-settings-section" key={item.id} disabled={busy || loading || !item.controllable}>
          <legend>{names[item.id] ?? item.title}</legend>
          {Object.entries(values).map(([key, value]) => <label key={key} className="dsh-setting-row"><span>{names[key] ?? key}<small>{tips[key] ?? (item.detail || "由 DSH 原生运行时应用。")}</small></span>
            {typeof value === "boolean" ? <input type="checkbox" aria-label={names[key] ?? key} checked={Boolean((draft?.values ?? values)[key])} onChange={event => change(key, event.target.checked)} />
              : <input aria-label={names[key] ?? key} required type={typeof value === "number" ? "number" : "text"} min={constraints[key]?.min} max={constraints[key]?.max} step={constraints[key]?.step ?? "any"} value={String((draft?.values ?? values)[key] ?? "")} onChange={event => change(key, event.target.value)} />}
          </label>)}
          {!item.controllable && <small className="dsh-management-meta">当前不可修改，请刷新确认运行时状态。</small>}
        </fieldset>;
      })}
      {loading && <p role="status" className="dsh-feature-empty">读取中…</p>}
      {!loading && !error && !items.length && <p className="dsh-feature-empty">当前运行时没有开放可编辑的执行设置</p>}
      {notice && <p role="status" className="dsh-management-notice">{notice}</p>}
    </div>
    <footer className="dsh-settings-footer"><span>{count ? `已修改 ${count} 项` : "保存后生效"}</span>
      {!!count && <button type="button" disabled={busy} onClick={() => { setDrafts({}); setNotice(""); setRevision(value => value + 1); }}>撤销</button>}
      <button className="dsh-primary" disabled={!count || busy || loading}>{busy ? "保存中…" : "保存修改"}</button>
    </footer>
  </form>;
}
