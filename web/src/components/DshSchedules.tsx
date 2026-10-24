import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { Icon } from "../icons";
import { ChoicePicker } from "./ChoicePicker";
import { DateTimePicker } from "./DateTimePicker";

const rules = [
  { value: "at", label: "指定时间", description: "只执行一次" },
  { value: "every", label: "固定间隔", description: "每隔一段时间执行" },
  { value: "daily", label: "每天" }, { value: "weekly", label: "每周" },
  { value: "cron", label: "自定义 Cron", description: "使用五段式表达式" },
];
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
const dateLabel = (value: unknown) => {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.getTime()) ? "时间待确认" : date.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
};
function description(item: DshItem) {
  const d = item.data ?? {};
  if (d.kind === "every") return `每 ${Number(d.everySeconds) / 60} 分钟`;
  if (d.kind === "daily") return `每天 ${String(d.time).slice(0, 5)}`;
  if (d.kind === "weekly") return `每周${(d.weekdays as number[] ?? []).map(n => weekdays[n - 1]).join("、")} ${String(d.time).slice(0, 5)}`;
  if (d.kind === "cron") return String(d.expression);
  return "单次任务";
}

export function DshSchedules({ sid, api, onCompose }: { sid: string; api: DshApiClient; onCompose: (prompt: string) => void }) {
  const [items, setItems] = useState<DshItem[]>([]), [sessions, setSessions] = useState<DshItem[]>([]);
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<DshItem | "new" | null>(null);
  const [menu, setMenu] = useState("");
  const [remove, setRemove] = useState("");
  const [history, setHistory] = useState<{ id: string; rows: DshItem[] } | null>(null);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true);
    Promise.all([api.read(sid, "schedules", { signal: abort.signal }),
      api.read(sid, "schedules", { query: "sessions", signal: abort.signal })]).then(([list, catalog]) => {
      if (abort.signal.aborted) return;
      setItems(list.items); setSessions(catalog.items);
      if (list.error || catalog.error) setNotice(list.error || catalog.error || "");
    }).catch(error => { if (!abort.signal.aborted) setNotice(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, api, revision]);
  const mutate = async (body: Record<string, unknown>) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify(body));
      setNotice(result.text);
      if (result.status !== "error") { setEditing(null); setRemove(""); }
    } catch (error) {
      setNotice(`${(error as Error).message}；请先刷新核实结果，操作不会自动重发。`); setEditing(null);
    } finally { setBusy(false); setRevision(n => n + 1); }
  };
  const showHistory = async (item: DshItem) => {
    setMenu(""); setBusy(true);
    try {
      const result = await api.read(sid, "schedule_history", { query: item.id, target_sid: item.sid ?? undefined });
      if (result.error) throw new Error(result.error);
      setHistory({ id: item.id, rows: result.items });
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="dsh-management dsh-schedules">
    <div className="dsh-schedules-heading"><span>{editing ? editing === "new" ? "新建任务" : "编辑任务" : "按计划，继续你的工作"}</span>
      {!editing && <><button className="iconbtn" aria-label="刷新任务" disabled={loading || busy} onClick={() => setRevision(n => n + 1)}><Icon name="refresh" size={17} /></button>
        {!!items.length && <button className="dsh-primary" disabled={busy} onClick={() => setEditing("new")}><Icon name="plus" size={16} />新建任务</button>}</>}
    </div>
    <div className="dsh-management-content">
      {notice && <p role="status" className="dsh-management-notice">{notice}</p>}
      {editing ? <ScheduleEditor key={typeof editing === "string" ? "new" : editing.id} item={editing === "new" ? undefined : editing}
        sid={sid} sessions={sessions} busy={busy} onCancel={() => setEditing(null)}
        onSave={body => void mutate(body)} /> : <>
        {loading && !items.length && <p role="status" className="dsh-feature-empty">读取中…</p>}
        {!loading && !items.length && <div className="dsh-schedule-empty"><span><Icon name="clock" size={28} /></span>
          <h3>把下一件事交给计划</h3><p>选择时间，让 DSH 在指定会话中继续工作。</p>
          <button className="dsh-primary" onClick={() => setEditing("new")}><Icon name="plus" size={17} />新建任务</button>
          <button className="dsh-schedule-converse" onClick={() => onCompose("请帮我创建一个定时任务，先和我确认任务内容、时间和时区。")}>也可以通过对话创建</button>
        </div>}
        {items.map(item => <article className="dsh-schedule-row" key={item.id}>
          <div className="dsh-schedule-title"><span className="dsh-schedule-symbol"><Icon name="clock" size={19} /></span>
            <div><strong>{item.title}</strong><small>{description(item)}{item.data?.timeZone ? ` · ${item.data.timeZone}` : ""}</small></div>
            <span className="dsh-schedule-state" data-active={item.state === "active"}>{item.state === "active" ? "已安排" : "已结束"}</span>
            <button className="iconbtn" aria-label={`${item.title}的操作`} aria-expanded={menu === item.id} onClick={() => setMenu(menu === item.id ? "" : item.id)}><Icon name="dots" size={18} /></button>
          </div>
          <p>{item.detail}</p>
          <div className="dsh-schedule-meta"><span>{item.state === "active" ? "下次执行" : "计划时间"} · {dateLabel(item.data?.scheduledAt)}</span>
            <span><Icon name="chat" size={13} />{sessions.find(s => s.id === item.sid)?.title ?? "所属会话"}</span></div>
          {menu === item.id && <div className="dsh-schedule-menu"><button disabled={busy || item.state !== "active"} onClick={() => { setEditing(item); setMenu(""); }}>编辑任务</button><button disabled={busy} onClick={() => void showHistory(item)}>执行记录</button><button disabled={busy} onClick={() => { setRemove(item.id); setMenu(""); }}>删除任务</button></div>}
          {remove === item.id && <div className="dsh-inline-confirm"><p>删除这个定时任务？</p><button disabled={busy} onClick={() => setRemove("")}>取消</button><button disabled={busy} onClick={() => void mutate({ action: "schedule_delete", sid: item.sid, id: item.id })}>确认删除</button></div>}
          {history?.id === item.id && <div className="dsh-deliveries"><strong>执行记录</strong><p>记录表示指令已送达会话，任务完成情况请查看会话回复。</p>{history.rows.map(row => <p key={row.id}>{dateLabel(row.title)} · 已送达</p>)}{!history.rows.length && <p>还没有执行记录</p>}<button onClick={() => setHistory(null)}>收起</button></div>}
        </article>)}
      </>}
    </div>
  </div>;
}

function ScheduleEditor({ item, sid, sessions, busy, onSave, onCancel }: { item?: DshItem; sid: string; sessions: DshItem[]; busy: boolean; onSave: (body: Record<string, unknown>) => void; onCancel: () => void }) {
  const [title, setTitle] = useState(item?.title ?? ""), [prompt, setPrompt] = useState(item?.detail ?? "");
  const [target, setTarget] = useState(item?.sid ?? sid);
  const [kind, setKind] = useState(item ? "keep" : "daily"), [time, setTime] = useState("09:00");
  const [date, setDate] = useState(""), [minutes, setMinutes] = useState("60"), [cron, setCron] = useState("0 9 * * 1-5");
  const [zone, setZone] = useState(String(item?.data?.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone));
  const [days, setDays] = useState<number[]>([1, 2, 3, 4, 5]);
  const choices = item ? [{ value: "keep", label: "保持当前时间" }, ...rules] : rules;
  const save = () => {
    const timing = kind === "at" ? { at: { date: date.split("T")[0], time: date.split("T")[1] + ":00", time_zone: zone } }
      : kind === "every" ? { every_seconds: Number(minutes) * 60 }
      : kind === "daily" ? { daily: { time: time + ":00", time_zone: zone } }
      : kind === "weekly" ? { weekly: { time: time + ":00", time_zone: zone, weekdays: days } }
      : { cron: { expression: cron, time_zone: zone } };
    onSave(item ? { action: "schedule_update", sid: item.sid, id: item.id, expected: item.data, title, prompt,
      ...(kind === "keep" ? {} : { change: { kind, ...timing } }) }
      : { action: "schedule_create", sid: target, request: { title, prompt, ...timing } });
  };
  return <form className="dsh-schedule-editor" onSubmit={event => { event.preventDefault(); save(); }}>
    <label>任务名称<input required value={title} maxLength={120} placeholder="例如：检查项目进展" onChange={e => setTitle(e.target.value)} /></label>
    <label>任务内容<textarea required value={prompt} maxLength={24000} placeholder="到时间后，你希望 DSH 做什么？" onChange={e => setPrompt(e.target.value)} /></label>
    {!item && <div className="dsh-schedule-field"><span>目标会话</span><ChoicePicker label="选择目标会话" value={target}
      options={sessions.map(s => ({ value: s.id, label: s.title, description: s.detail }))} onChange={setTarget}>
      {sessions.find(s => s.id === target)?.title ?? "当前会话"}</ChoicePicker></div>}
    <div className="dsh-schedule-field"><span>重复规则</span><ChoicePicker label="选择重复规则" value={kind} options={choices} onChange={setKind}>{choices.find(c => c.value === kind)?.label}</ChoicePicker></div>
    {kind === "at" && <DateTimePicker value={date} onChange={setDate} />}
    {kind === "every" && <label>间隔（分钟）<input type="number" min={1} step={1} required value={minutes} onChange={e => setMinutes(e.target.value)} /></label>}
    {["daily", "weekly"].includes(kind) && <label>执行时间<input aria-label="执行时间" required value={time} placeholder="09:00" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" onChange={e => setTime(e.target.value)} /></label>}
    {kind === "weekly" && <div className="dsh-schedule-week" role="group" aria-label="每周执行日期">{weekdays.map((d, i) => <button type="button" key={d} aria-pressed={days.includes(i + 1)} onClick={() => setDays(old => old.includes(i + 1) ? old.filter(n => n !== i + 1) : [...old, i + 1].sort())}>{d}</button>)}</div>}
    {kind === "cron" && <label>Cron 表达式<input required value={cron} onChange={e => setCron(e.target.value)} /><small>分钟 · 小时 · 日 · 月 · 星期</small></label>}
    {["at", "daily", "weekly", "cron"].includes(kind) && <label>时区<input required value={zone} placeholder="Asia/Shanghai" onChange={e => setZone(e.target.value)} /></label>}
    <div className="dsh-schedule-form-actions"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button className="dsh-primary" disabled={busy || !title.trim() || !prompt.trim() || (kind === "at" && !date) || (kind === "weekly" && !days.length)}>{busy ? "正在保存…" : item ? "保存修改" : "创建任务"}</button></div>
  </form>;
}
