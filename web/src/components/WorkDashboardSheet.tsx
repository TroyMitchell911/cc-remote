import { useEffect, useState } from "react";
import { Icon } from "../icons";
import type { ClaudeProfileInfo, CodexProfileInfo, QueryFile, WorkDashboard } from "../protocol";
import { MAX_FILENAME_BYTES, MAX_SINGLE_ATTACHMENT_BYTES } from "../img";
import { DateTimePicker } from "./DateTimePicker";
import {
  resolveWorkScheduleProfile, workProfileDisplayName,
} from "../work-profile-selection";

type Tab = "projects" | "library" | "schedules" | "plugins";
const EMPTY_CODEX_PROFILES: CodexProfileInfo[] = [];
const EMPTY_CLAUDE_PROFILES: ClaudeProfileInfo[] = [];

interface Props {
  open: boolean;
  scopeKey: string;
  dashboard: WorkDashboard | null;
  claudeProfiles?: ClaudeProfileInfo[];
  defaultClaudeProfileId?: string | null;
  claudeProfileId?: string | null;
  codexProfiles?: CodexProfileInfo[];
  defaultCodexProfileId?: string | null;
  codexProfileId?: string | null;
  selectedProjectId: string | null;
  onSelectProject: (projectId: string | null) => void;
  onClose: () => void;
  onCreateProject: (name: string, description: string) => boolean;
  onDeleteProject: (projectId: string) => boolean;
  onAddSource: (projectId: string, kind: "file" | "link" | "note",
    title: string, uri?: string, file?: QueryFile) => boolean;
  onDeleteSource: (sourceId: string) => boolean;
  onCreateSchedule: (title: string, prompt: string, nextRunAt: number,
    repeatSeconds?: number, projectId?: string,
    codexProfileId?: string) => boolean;
  onDeleteSchedule: (scheduleId: string) => boolean;
  onCreatePlugin: (name: string, instructions: string, projectId?: string) => boolean;
  onDeletePlugin: (pluginId: string) => boolean;
}

function toQueryFile(file: File): Promise<QueryFile> {
  if (file.size > MAX_SINGLE_ATTACHMENT_BYTES) {
    return Promise.reject(new Error(`${file.name} 超过 6 MiB`));
  }
  if (!file.name || new TextEncoder().encode(file.name).byteLength > MAX_FILENAME_BYTES) {
    return Promise.reject(new Error("文件名为空或过长"));
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      filename: file.name,
      data: String(reader.result || "").split(",", 2)[1] || "",
    });
    reader.onerror = () => reject(new Error(`${file.name} 读取失败`));
    reader.readAsDataURL(file);
  });
}

export function WorkDashboardSheet(props: Props) {
  const { open, dashboard, selectedProjectId, onSelectProject, onClose } = props;
  const [tab, setTab] = useState<Tab>("projects");
  const [name, setName] = useState("");
  const [detail, setDetail] = useState("");
  const [sourceTitle, setSourceTitle] = useState("");
  const [sourceBody, setSourceBody] = useState("");
  const [scheduleTitle, setScheduleTitle] = useState("");
  const [schedulePrompt, setSchedulePrompt] = useState("");
  const [scheduleAt, setScheduleAt] = useState("");
  const [repeat, setRepeat] = useState("");
  const [scheduleProfileId, setScheduleProfileId] = useState<string | null>(null);
  const [pluginName, setPluginName] = useState("");
  const [pluginInstructions, setPluginInstructions] = useState("");
  const [error, setError] = useState<string | null>(null);

  const codexProfiles = props.codexProfiles ?? EMPTY_CODEX_PROFILES;
  const claudeProfiles = props.claudeProfiles ?? EMPTY_CLAUDE_PROFILES;
  const accountProfiles = dashboard?.engine === "codex"
    ? codexProfiles : claudeProfiles;
  const defaultAccountProfileId = dashboard?.engine === "codex"
    ? props.defaultCodexProfileId : props.defaultClaudeProfileId;
  const preferredProfileId = (dashboard?.engine === "codex"
    ? props.codexProfileId : props.claudeProfileId)
    ?? defaultAccountProfileId
    ?? accountProfiles[0]?.id
    ?? null;
  const scheduleProfile = resolveWorkScheduleProfile(
    accountProfiles, scheduleProfileId, preferredProfileId);
  const effectiveScheduleProfileId = scheduleProfile.profileId;
  const scheduleProfileMissing = !!dashboard && scheduleProfile.missing;

  useEffect(() => {
    if (!open) setScheduleProfileId(null);
  }, [open]);

  useEffect(() => {
    // Drafts and account choices may outlive a close/reopen on one machine,
    // but they must never cross a device or engine ownership boundary.
    setTab("projects");
    setName("");
    setDetail("");
    setSourceTitle("");
    setSourceBody("");
    setScheduleTitle("");
    setSchedulePrompt("");
    setScheduleAt("");
    setRepeat("");
    setScheduleProfileId(null);
    setPluginName("");
    setPluginInstructions("");
    setError(null);
  }, [props.scopeKey]);

  if (!open) return null;
  const projects = dashboard?.projects ?? [];
  const libraryProjectId = selectedProjectId ?? projects[0]?.project_id ?? null;
  const projectName = (id?: string | null) =>
    projects.find((project) => project.project_id === id)?.name ?? "未归类";
  const profileName = (id?: string | null) => workProfileDisplayName(
    accountProfiles, defaultAccountProfileId, id);

  const remove = (label: string, action: () => boolean) => {
    if (window.confirm(`确定删除${label}吗？`)) action();
  };

  return (
    <>
      <div className="scrim show work-manager-scrim" onClick={onClose} />
      <div className="work-manager" role="dialog" aria-modal="true" aria-label="Work 工作台">
        <header>
          <div><span>Work 工作台</span><small>项目资料会在创建工作时复制进私有空间</small></div>
          <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
        </header>
        <nav>
          {([ ["projects", "项目"], ["library", "资料库"],
            ["schedules", "定时任务"], ["plugins", "工作模板"] ] as [Tab, string][]).map(([id, label]) => (
            <button key={id} className={tab === id ? "active" : ""}
              onClick={() => { setTab(id); setError(null); }}>{label}</button>
          ))}
        </nav>
        <div className="work-manager-body">
          {!dashboard && <div className="work-empty">正在读取 Work 数据…</div>}
          {error && <div className="work-form-error">{error}</div>}

          {dashboard && tab === "projects" && <>
            <section className="work-form">
              <h3>新建项目</h3>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="项目名称" />
              <textarea value={detail} onChange={(e) => setDetail(e.target.value)} placeholder="目标和背景（可选）" />
              <button className="primary" disabled={!name.trim()} onClick={() => {
                if (props.onCreateProject(name.trim(), detail.trim())) { setName(""); setDetail(""); }
              }}>创建项目</button>
            </section>
            <section className="work-items">
              {projects.map((project) => <article key={project.project_id}
                className={project.project_id === selectedProjectId ? "selected" : ""}
                onClick={() => onSelectProject(project.project_id)}>
                <div><b>{project.name}</b><p>{project.description || "暂无说明"}</p></div>
                <button className="danger" onClick={(e) => { e.stopPropagation(); remove(`项目「${project.name}」`, () => props.onDeleteProject(project.project_id)); }}><Icon name="trash" size={15} /></button>
              </article>)}
              {!projects.length && <div className="work-empty">还没有项目。工作也可以不归入项目。</div>}
            </section>
          </>}

          {dashboard && tab === "library" && <>
            <ProjectPicker projects={projects} value={libraryProjectId} onChange={onSelectProject} />
            {!libraryProjectId ? <div className="work-empty">先创建或选择一个项目，再添加资料。</div> : <>
              <section className="work-form">
                <h3>添加链接或笔记</h3>
                <input value={sourceTitle} onChange={(e) => setSourceTitle(e.target.value)} placeholder="资料名称" />
                <textarea value={sourceBody} onChange={(e) => setSourceBody(e.target.value)} placeholder="https://… 或一段长期参考说明" />
                <div className="work-form-actions">
                  <button disabled={!sourceTitle.trim() || !sourceBody.trim()} onClick={() => {
                    const kind = /^https?:\/\//i.test(sourceBody.trim()) ? "link" : "note";
                    if (props.onAddSource(libraryProjectId, kind, sourceTitle.trim(), sourceBody.trim())) {
                      setSourceTitle(""); setSourceBody("");
                    }
                  }}>添加资料</button>
                  <label className="file-action">上传文件<input type="file" hidden onChange={async (event) => {
                    const file = event.target.files?.[0]; event.target.value = "";
                    if (!file) return;
                    try { props.onAddSource(libraryProjectId, "file", file.name, undefined, await toQueryFile(file)); }
                    catch (reason) { setError(reason instanceof Error ? reason.message : "文件读取失败"); }
                  }} /></label>
                </div>
              </section>
              <section className="work-items">
                {dashboard.sources.filter((source) => source.project_id === libraryProjectId).map((source) => <article key={source.source_id}>
                  <div><b>{source.title}</b><p>{source.kind === "file" ? "文件" : source.uri}</p></div>
                  <button className="danger" onClick={() => remove(`资料「${source.title}」`, () => props.onDeleteSource(source.source_id))}><Icon name="trash" size={15} /></button>
                </article>)}
              </section>
            </>}
          </>}

          {dashboard && tab === "schedules" && <>
            <section className="work-form">
              <h3>新建定时任务</h3>
              <ProjectPicker projects={projects} value={selectedProjectId} onChange={onSelectProject} allowNone />
              {(accountProfiles.length > 1 || scheduleProfileMissing) && (
                <label className="work-schedule-profile">
                  <span>执行账号</span>
                  <select value={effectiveScheduleProfileId ?? ""}
                    onChange={(event) => setScheduleProfileId(event.target.value)}>
                    {scheduleProfileMissing && effectiveScheduleProfileId && (
                      <option value={effectiveScheduleProfileId} disabled>
                        已移除账号
                      </option>
                    )}
                    {accountProfiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profileName(profile.id)}
                        {profile.error ? " · 目录暂不可用" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {scheduleProfileMissing && (
                <p className="work-form-error">
                  所选 {dashboard.engine === "codex" ? "Codex" : "Claude"}
                  账号已移除，请重新选择执行账号。
                </p>
              )}
              <input value={scheduleTitle} onChange={(e) => setScheduleTitle(e.target.value)} placeholder="任务名称" />
              <textarea value={schedulePrompt} onChange={(e) => setSchedulePrompt(e.target.value)} placeholder="到时间后让 Agent 完成什么？" />
              <div className="work-form-actions">
                <DateTimePicker value={scheduleAt} onChange={setScheduleAt} />
                <select value={repeat} onChange={(e) => setRepeat(e.target.value)}>
                  <option value="">仅一次</option><option value="86400">每天</option><option value="604800">每周</option>
                </select>
              </div>
              <button className="primary" disabled={!scheduleTitle.trim() || !schedulePrompt.trim() || !scheduleAt
                || !effectiveScheduleProfileId || scheduleProfileMissing} onClick={() => {
                const when = new Date(scheduleAt).getTime() / 1000;
                if (!Number.isFinite(when) || when < Date.now() / 1000 - 60) { setError("请选择未来的执行时间"); return; }
                if (props.onCreateSchedule(scheduleTitle.trim(), schedulePrompt.trim(), when,
                  repeat ? Number(repeat) : undefined, selectedProjectId ?? undefined,
                  effectiveScheduleProfileId ?? undefined)) {
                  setScheduleTitle(""); setSchedulePrompt(""); setScheduleAt("");
                }
              }}>创建任务</button>
            </section>
            <section className="work-items">
              {dashboard.schedules.map((item) => <article key={item.schedule_id}>
                <div><b>{item.title}</b><p>{projectName(item.project_id)} · {profileName(dashboard.engine === "codex" ? item.codex_profile_id : item.claude_profile_id)} · {item.enabled ? new Date(item.next_run_at * 1000).toLocaleString() : "已执行"}{item.last_error ? ` · ${item.last_error}` : ""}</p></div>
                <button className="danger" onClick={() => remove(`任务「${item.title}」`, () => props.onDeleteSchedule(item.schedule_id))}><Icon name="trash" size={15} /></button>
              </article>)}
            </section>
          </>}

          {dashboard && tab === "plugins" && <>
            <section className="work-form">
              <h3>新建工作模板</h3>
              <ProjectPicker projects={projects} value={selectedProjectId} onChange={onSelectProject} allowNone />
              <input value={pluginName} onChange={(e) => setPluginName(e.target.value)} placeholder="模板名称" />
              <textarea value={pluginInstructions} onChange={(e) => setPluginInstructions(e.target.value)} placeholder="告诉 Agent 这套模板的工作规范、格式或处理方法" />
              <button className="primary" disabled={!pluginName.trim() || !pluginInstructions.trim()} onClick={() => {
                if (props.onCreatePlugin(pluginName.trim(), pluginInstructions.trim(), selectedProjectId ?? undefined)) {
                  setPluginName(""); setPluginInstructions("");
                }
              }}>添加模板</button>
              <small>工作模板是会进入 WORK.md 的可复用说明，不冒充或执行引擎插件。真实 Skills、Plugins 与连接请使用 /extensions 查看。</small>
            </section>
            <section className="work-items">
              {dashboard.plugins.map((item) => <article key={item.plugin_id}>
                <div><b>{item.name}</b><p>{projectName(item.project_id)} · {item.instructions}</p></div>
                <button className="danger" onClick={() => remove(`模板「${item.name}」`, () => props.onDeletePlugin(item.plugin_id))}><Icon name="trash" size={15} /></button>
              </article>)}
            </section>
          </>}
        </div>
      </div>
    </>
  );
}

function ProjectPicker({ projects, value, onChange, allowNone = false }: {
  projects: WorkDashboard["projects"];
  value: string | null;
  onChange: (projectId: string | null) => void;
  allowNone?: boolean;
}) {
  return <select className="work-project-picker" value={value ?? ""}
    onChange={(event) => onChange(event.target.value || null)}>
    {allowNone && <option value="">不归入项目</option>}
    {!allowNone && !projects.length && <option value="">暂无项目</option>}
    {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
  </select>;
}
