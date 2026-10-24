import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { Icon } from "../icons";
import { DshSchedules } from "./DshSchedules";
import { DshBundleRow } from "./DshBundleRow";
import { bundlePresentation } from "../dsh-bundle-presentation";

type Kind = "schedules" | "presets" | "bundles";
const status: Record<string, string> = { active: "生效中", inactive: "已结束", enabled: "已启用", disabled: "未启用", failed: "配置异常", default: "默认", available: "可用", "not-installed": "可安装", installed: "已安装", provided: "内置", "update-available": "有新版本", "different-version": "版本不同" };
const captions = {
  presets: "为新会话选择起点。已创建的会话继续使用自己的预设。",
};

/** Management writes only happen on a user's explicit Save / Install action. */
export function DshManagement(props: { sid: string; kind: Kind; api: DshApiClient; onCompose: (prompt: string, preset?: string) => void }) {
  return props.kind === "schedules" ? <DshSchedules {...props} /> : <Management {...props} kind={props.kind} />;
}
function Management({ sid, kind, api, onCompose }: {
  sid: string; kind: Exclude<Kind, "schedules">; api: DshApiClient;
  onCompose: (prompt: string, preset?: string) => void;
}) {
  const [items, setItems] = useState<DshItem[]>([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [spec, setSpec] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const [market, setMarket] = useState(false);
  const [search, setSearch] = useState("");
  const showMarket = kind === "bundles" && market;
  const [offset, setOffset] = useState<number | undefined>();
  const [next, setNext] = useState<number | null>(null);
  const [remove, setRemove] = useState<DshItem | null>(null);
  useEffect(() => {
    if (showMarket) return;
    const abort = new AbortController();
    setLoading(true); setError(""); setNext(null);
    if (!offset) setItems([]);
    api.read(sid, kind, { signal: abort.signal, before_seq: offset }).then(result => {
      if (abort.signal.aborted) return;
      setItems(previous => offset ? [...previous, ...result.items.filter(item => !previous.some(old => old.id === item.id))] : result.items);
      setNext(result.has_more ? result.next_seq ?? null : null); setError(result.error ?? "");
    }).catch(error => { if (!abort.signal.aborted) setError(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, kind, api, revision, showMarket, offset]);
  const mutate = async (body: Record<string, unknown>) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify(body));
      setNotice(result.text);
      if (result.status === "success") { setRemove(null); setSpec(""); }
      // Never retry a possibly accepted mutation. Read authoritative state.
      setOffset(undefined); setRevision(value => value + 1);
    } catch (error) { setNotice(`${(error as Error).message}；请刷新核实结果，操作不会自动重发。`); }
    finally { setBusy(false); }
  };
  const makeDefault = async (item: DshItem) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.read(sid, "settings");
      const row = result.items.find(row => row.id === "agent-preset-registry" && row.controllable);
      if (!row) throw new Error(result.error || "当前运行时未开放默认预设设置");
      await mutate({ action: "preset_default", id: item.id, revision: row.data?.revision });
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  const visible = items.filter(item => kind !== "bundles" || `${item.id} ${bundlePresentation(item).title} ${bundlePresentation(item).detail} ${item.detail}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const catalogTabs = <div className="dsh-catalog-tabs">
    <button aria-pressed={!market} disabled={busy} onClick={() => { setMarket(false); setOffset(undefined); }}>本机插件</button>
    <button aria-pressed={market} disabled={busy} onClick={() => { setMarket(true); setOffset(undefined); }}>插件市场</button>
    {!market && <button className="iconbtn" aria-label="刷新插件" disabled={loading || busy} onClick={() => { setOffset(undefined); setRevision(value => value + 1); }}><Icon name="refresh" size={17} /></button>}
  </div>;
  if (showMarket) return <div className="dsh-management dsh-bundles">
    <div className="dsh-management-content">
      {catalogTabs}
      <div className="dsh-market-unavailable">
        <span aria-hidden="true"><Icon name="archive" size={26} /></span>
        <h3>DSH 暂未提供官方插件市场</h3>
        <p>已安装的插件可在「本机插件」中管理。</p>
      </div>
    </div>
  </div>;
  return <div className={`dsh-management${kind === "bundles" ? " dsh-bundles" : ""}`}>
    {kind !== "bundles" && <div className="dsh-management-toolbar">
      <button disabled={loading || busy} onClick={() => { setOffset(undefined); setRevision(value => value + 1); }}>刷新</button>
      {kind === "presets" && <button className="dsh-primary" onClick={() => onCompose("我想创建一个 DSH 代理预设，请先和我确认用途及需要的能力。", "cordis")}>创建预设</button>}
    </div>}
    <div className="dsh-management-content">
      {kind !== "bundles" && <p className="dsh-management-caption">{captions[kind]}</p>}
      {notice && <p role="status" className="dsh-management-notice">{notice}</p>}
      {error && <p role="alert" className="dsh-feature-error">{error}</p>}
      {kind === "bundles" && <>
        {catalogTabs}
        <form className="dsh-catalog-search" onSubmit={event => event.preventDefault()}>
          <Icon name="search" size={18} /><input aria-label="搜索插件" placeholder="搜索名称或说明…" value={search} maxLength={200} onChange={event => setSearch(event.target.value)} />
          <button type="button" className="iconbtn" aria-label="添加插件包" aria-expanded={installOpen} onClick={() => setInstallOpen(value => !value)}><Icon name="plus" size={20} /></button>
        </form>
      </>}
      {kind === "bundles" && !market && installOpen && <form className="dsh-install" onSubmit={event => { event.preventDefault(); void mutate({ action: "bundle_install", spec }); }}>
        <label htmlFor="dsh-bundle-spec">从包名或路径安装</label><div><input id="dsh-bundle-spec" placeholder="包名或设备上的路径" value={spec} onChange={event => setSpec(event.target.value)} maxLength={2048} />
          <button className="dsh-primary" disabled={busy || !spec.trim()}>安装</button></div>
      </form>}
      {visible.map(item => <article className={kind === "bundles" ? "dsh-bundle-row" : "dsh-management-card"} key={item.id}>
        {kind === "bundles" ? <DshBundleRow item={item} busy={busy} onToggle={() => void mutate({ action: item.state === "enabled" ? "bundle_disable" : "bundle_enable", name: item.id })} onRemove={() => setRemove(item)} onInstall={() => void mutate({ action: "bundle_install", ...(item.data!.installTarget as Record<string, unknown>) })} /> : <>
        <div className="dsh-management-title"><strong>{item.title}</strong>{status[item.state] && <small>{status[item.state]}</small>}</div>
        {item.detail && <p>{item.detail}</p>}
        <div className="dsh-management-actions">
            {kind === "presets" && <><button disabled={busy || item.state === "failed"} onClick={() => onCompose("", item.id)}>用此预设新建会话</button><button disabled={busy || item.state !== "available"} onClick={() => void makeDefault(item)}>设为默认</button></>}
          </div></>}
        {remove?.id === item.id && <div className="dsh-inline-confirm"><p>卸载这个插件包？正在使用的插件由 DSH 决定是否允许卸载。</p><button disabled={busy} onClick={() => setRemove(null)}>取消</button><button disabled={busy} onClick={() => void mutate({ action: "bundle_remove", name: item.id })}>确认卸载</button></div>}
      </article>)}
      {kind === "bundles" && !market && !installOpen && <button className="dsh-install-link" onClick={() => setInstallOpen(true)}><Icon name="plus" size={17} />从包名或路径安装</button>}
      {next !== null && <button disabled={loading || busy} onClick={() => setOffset(next)}>加载更多插件</button>}
      {loading && <p role="status" className="dsh-feature-empty">读取中…</p>}
      {!loading && !error && !visible.length && <p className="dsh-feature-empty">{kind === "bundles" ? "没有匹配的插件" : "暂无记录"}</p>}
    </div>
  </div>;
}
