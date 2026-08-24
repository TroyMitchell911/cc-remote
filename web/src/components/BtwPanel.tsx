import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type SetStateAction,
} from "react";
import { ChatView } from "./ChatView";
import { CommandSheet } from "./CommandSheet";
import { Icon } from "../icons";
import { PanelTabs } from "./PanelTabs";
import { NoticeStack } from "./NoticeStack";
import type {
  Artifact,
  PendingQuery,
  PreviewAuthorizationState,
  SessionRuntime,
} from "../reducer";
import type { ComposerDraft, ComposerDraftStore } from "../composer-drafts";
import { ImeSubmitGuard } from "../ime-submit";
import {
  classifyBusySubmit,
  isComposerBusy,
  isInterruptSettling,
  isSettlingStopDisabled,
  type SendMode,
} from "../composer-submit";
import { canEnqueueQuery, type QueueCapacity } from "../runtime-drain";
import {
  effortNameForDisplay, modelsFor, parseSlash, type Catalog,
} from "../data";
import { QueuedQueryChip } from "./QueuedQueryDialog";
import type { InlineImageAsset } from "../inline-image-assets";
import {
  composePastePrompt,
  LONG_PASTE_THRESHOLD,
  makeComposerPaste,
} from "../composer-pastes";
import { PasteCards } from "./PasteCards";
import { uuid } from "../util";
import {
  activeTurnCandidateIds,
  displayActiveTurnOwnerId,
} from "../process-blocks";
import { PanelResizer } from "./PanelResizer";
import {
  autoCompactSelectionLabel,
  parseAutoCompactArgument,
  type AutoCompactSelection,
} from "../auto-compact";
import { AutoCompactControl } from "./AutoCompactControl";

interface Props {
  sid?: string;
  rt: SessionRuntime | undefined;
  engine?: string;
  opening?: boolean;
  active: "diff" | "btw";
  hasArtifact: boolean;
  artifactKind?: Artifact["kind"];
  catalog: Catalog;
  draftKey: string;
  draftStore: ComposerDraftStore;
  sendMode: SendMode;
  unconfirmedQueued: PendingQuery[];
  unconfirmedReplaceable: PendingQuery[];
  queueCapacity: QueueCapacity;
  replaceQueueCapacity: QueueCapacity;
  onTab: (v: "diff" | "btw") => void;
  onSend: (prompt: string) => boolean;
  onSteer: (prompt: string) => boolean;
  onInterrupt: () => void;
  onSetSendMode: (mode: SendMode) => void;
  onEnqueue: (query: PendingQuery) => boolean;
  onSetPending: (query: PendingQuery) => boolean;
  onRemoveQueued: (query: PendingQuery) => void;
  onInspectQueued: (query: PendingQuery) => void;
  onSetModel: (model: string) => void;
  onSetEffort: (effort: string) => void;
  onSetAutoCompact: (selection: AutoCompactSelection) => boolean;
  onOpenFile?: (path: string, line?: number) => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onClose: () => void;
  onDismissNotice: (noticeId: string) => void;
}

/** /btw side panel: a private mini-chat over an ephemeral session fork.
 *
 * Its draft, busy-send intent, queue and controls are all keyed by the explicit
 * fork sid supplied by App. Nothing here is allowed to follow the main
 * session's current focus.
 */
export function BtwPanel(p: Props) {
  const draftKeyRef = useRef(p.draftKey);
  const [draft, setDraft] = useState<ComposerDraft>(
    () => p.draftStore.get(p.draftKey));
  const [sheetKind, setSheetKind] =
    useState<"models" | "efforts" | null>(null);
  const [autoCompactOpen, setAutoCompactOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimerRef = useRef<number | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);
  const input = draft.input;
  const turns = p.rt?.turns ?? [];
  const runtimeState = p.rt?.state ?? "idle";
  const activeTurnCandidates = activeTurnCandidateIds(
    turns,
    displayActiveTurnOwnerId(
      p.rt?.liveOwner?.turnId,
      p.rt?.acceptancePending,
    ),
    runtimeState !== "idle" || p.rt?.mirroredRunning === true
      || !!p.rt?.acceptancePending,
  );
  const activeTurnId = activeTurnCandidates.length === 1
    ? activeTurnCandidates[0] : null;
  // The query outbox can be awaiting its first authoritative echo while the
  // last lifecycle frame still says idle. Treat that short acceptance window
  // as settling-busy so a second submit becomes pending/queued instead of
  // racing another Query against the same fork.
  const acceptancePending = !!p.rt?.acceptancePending;
  const submitState = runtimeState === "idle" && acceptancePending
    ? "draining" : runtimeState;
  const runtimeBusy = isComposerBusy(submitState);
  const busy = !!p.opening || runtimeBusy;
  const hasText = input.trim().length > 0 || draft.pastes.length > 0;

  const updateDraft = useCallback((
    update: (current: ComposerDraft) => ComposerDraft,
  ) => {
    const key = draftKeyRef.current;
    setDraft((current) => {
      const next = update(current);
      p.draftStore.set(key, next);
      return next;
    });
  }, [p.draftStore]);
  const setInput = useCallback((next: SetStateAction<string>) => {
    updateDraft((current) => ({
      ...current,
      input: typeof next === "function" ? next(current.input) : next,
    }));
  }, [updateDraft]);
  const clearDraft = useCallback(() => {
    updateDraft(() => ({ input: "", images: [], files: [], pastes: [] }));
  }, [updateDraft]);

  useLayoutEffect(() => {
    if (draftKeyRef.current === p.draftKey) return;
    draftKeyRef.current = p.draftKey;
    setDraft(p.draftStore.get(p.draftKey));
    setSheetKind(null);
    setAutoCompactOpen(false);
    setNotice(null);
    if (noticeTimerRef.current !== null) {
      window.clearTimeout(noticeTimerRef.current);
      noticeTimerRef.current = null;
    }
  }, [p.draftKey, p.draftStore]);

  useEffect(() => {
    if (busy) setSheetKind(null);
  }, [busy]);

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
    if (noticeTimerRef.current !== null) {
      window.clearTimeout(noticeTimerRef.current);
    }
  }, []);

  const flash = (message: string) => {
    setNotice(message);
    if (noticeTimerRef.current !== null) {
      window.clearTimeout(noticeTimerRef.current);
    }
    noticeTimerRef.current = window.setTimeout(() => {
      noticeTimerRef.current = null;
      setNotice(null);
    }, 2200);
  };
  const resetTaHeight = () => {
    if (taRef.current) taRef.current.style.height = "auto";
  };
  const grow = (element: HTMLTextAreaElement) => {
    element.style.height = "auto";
    element.style.height = Math.min(element.scrollHeight, 120) + "px";
  };
  useLayoutEffect(() => {
    if (taRef.current) grow(taRef.current);
  }, [input, p.draftKey]);

  const submit = (value = taRef.current?.value ?? input) => {
    if (p.opening || !p.sid) return;
    const command = parseSlash(value.trim());
    if (command?.slash === "autocompact") {
      if (p.engine === "codex") {
        flash("自动压缩阈值仅适用于 Claude 会话。");
        setInput("");
        resetTaHeight();
        return;
      }
      if (!command.args) {
        setAutoCompactOpen(true);
        setInput("");
        resetTaHeight();
        return;
      }
      const parsed = parseAutoCompactArgument(command.args);
      if (!parsed.ok) {
        flash(parsed.error);
        return;
      }
      if (p.rt?.autoCompact && !p.rt.autoCompact.mutable) {
        flash("本机 Claude TUI 正在控制此会话，请在终端启动时设置。");
        return;
      }
      const queued = p.onSetAutoCompact(parsed.selection);
      flash(queued
        ? `正在应用自动压缩设置：${autoCompactSelectionLabel(parsed.selection)}`
        : "自动压缩设置暂未发送，请稍后重试。");
      setInput("");
      resetTaHeight();
      return;
    }
    const composed = composePastePrompt(draft.pastes, value.trim());
    if (!composed.ok) {
      flash(`消息内容超过上限（最多 ${composed.maxChars.toLocaleString()} 个字符）`);
      return;
    }
    const prompt = composed.prompt;
    const query: PendingQuery = { prompt };
    if (runtimeBusy) {
      const action = classifyBusySubmit(
        submitState, p.sendMode,
        p.engine === "codex" ? "codex" : "claude",
        prompt.length > 0);
      if (action === "noop") return;
      if (action === "steer") {
        if (p.onSteer(prompt)) {
          clearDraft();
          resetTaHeight();
        }
        return;
      }
      const unconfirmed = p.sendMode === "queue"
        ? p.unconfirmedQueued : p.unconfirmedReplaceable;
      const capacity = p.sendMode === "queue"
        ? p.queueCapacity : p.replaceQueueCapacity;
      if (!canEnqueueQuery(unconfirmed, query, capacity)) {
        flash("排队已满（最多 32 条 / 64 MiB），请先等待发送");
        return;
      }
      if (action === "enqueue") {
        if (!p.onEnqueue(query)) return;
      } else {
        if (action === "interrupt-and-replace") p.onInterrupt();
        if (!p.onSetPending(query)) return;
      }
      clearDraft();
      resetTaHeight();
      return;
    }
    if (!prompt) return;
    if (p.onSend(prompt)) {
      clearDraft();
      resetTaHeight();
    }
  };
  const requestButtonAction = () => {
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      const value = taRef.current?.value ?? input;
      // Stopping is an explicit button action. Empty Enter goes through submit
      // and remains a no-op.
      if (runtimeBusy && !value.trim() && draft.pastes.length === 0) {
        if (runtimeState === "running") p.onInterrupt();
        return;
      }
      submit(value);
    }, 0);
  };

  const modelList = modelsFor(p.engine, p.catalog);
  const model = p.rt?.model
    ? (modelList.find((candidate) => candidate.id === p.rt?.model)
      ?? { id: p.rt.model, name: p.rt.model, ds: "", ic: "cpu" })
    : null;
  const effortName = effortNameForDisplay(p.rt?.effort);
  const stopping = runtimeBusy && !hasText;
  const interruptSettling = isInterruptSettling(submitState);
  const primaryIsInterrupt = p.engine !== "codex";
  const sendIcon = !runtimeBusy ? "send"
    : stopping ? "stop" : p.sendMode === "steer"
      ? (primaryIsInterrupt ? "bolt" : "send") : "queue";
  const sendClass = "btw-send"
    + ((stopping || (runtimeBusy && p.sendMode === "steer"
      && primaryIsInterrupt && hasText))
      ? " interrupt" : "");
  const sendDisabled = !!p.opening || !p.sid
    || (!runtimeBusy && !hasText)
    || isSettlingStopDisabled(submitState, hasText);

  return (
    <div className="btw-panel" data-lock-horizontal-swipe="true">
      <PanelResizer ariaLabel="调整 BTW 面板宽度" />
      <div className="btw-head">
        {p.hasArtifact
          ? <PanelTabs active={p.active} artifactKind={p.artifactKind}
              onTab={p.onTab} />
          : <div className="btw-titles">
              <span className="btw-title">
                btw · 侧边对话{p.engine === "codex" ? " · Codex" : ""}
              </span>
              <span className="btw-sub">基于当前会话上下文,不写回主线</span>
            </div>}
        <button className="iconbtn" onClick={p.onClose}
          aria-label="关闭 btw" title="关闭并丢弃这个侧边对话">
          <Icon name="chevrons-right" />
        </button>
      </div>
      <NoticeStack notices={p.rt?.notices ?? []}
        onDismiss={p.onDismissNotice} />
      <div className="btw-body">
        {p.opening
          ? <div className="btw-empty">
              <span className="thinking"><span/><span/><span/></span>
              {" "}正在打开侧边对话…
            </div>
          : turns.length === 0
            ? <div className="btw-empty">
                问一个基于当前会话的侧边问题 —— 回答不会写进主线,关闭即丢弃。
              </div>
            : <ChatView sid={p.sid ?? null} turns={turns}
                activeTurnId={activeTurnId}
                onEdit={() => {}} onGetDiff={() => {}}
                onOpenFile={p.onOpenFile}
                imageAssets={p.imageAssets}
                onLoadImage={p.onLoadImage}
                onAuthorizeImage={p.onAuthorizeImage}
                ambiguousActiveTurnIds={activeTurnCandidates.length > 1
                  ? activeTurnCandidates : []} />}
      </div>
      <div className="btw-composer">
        {notice && <div className="btw-composer-notice">{notice}</div>}
        {(
          (p.rt?.queue.length ?? 0) > 0
          || !!p.rt?.pendingSend
          || (p.rt?.failedDeferred.length ?? 0) > 0
        ) && (
          <div className="btw-queued">
            {[
              ...(p.rt?.pendingSend ? [p.rt.pendingSend] : []),
              ...(p.rt?.queue ?? []),
              ...(p.rt?.failedDeferred ?? []),
            ].map((query, index) => (
              <QueuedQueryChip query={query} key={query.msg_id ?? index}
                onOpen={p.onInspectQueued}
                onRemove={() => p.onRemoveQueued(query)} />
            ))}
          </div>
        )}
        {draft.pastes.length > 0 && (
          <div className="attach show btw-pastes">
            <PasteCards pastes={draft.pastes}
              disabled={!!p.opening || !p.sid}
              onChange={(pastes) => updateDraft((current) => ({
                ...current, pastes,
              }))} />
          </div>
        )}
        {runtimeBusy && (
          <div className="btw-runbar">
            <div className="seg">
              <button className={p.sendMode === "steer" ? "on" : ""}
                onClick={() => p.onSetSendMode("steer")}>
                <Icon name={primaryIsInterrupt ? "bolt" : "send"} size={14} />
                {primaryIsInterrupt ? "打断并发送" : "引导"}
              </button>
              <button className={p.sendMode === "queue" ? "on" : ""}
                onClick={() => p.onSetSendMode("queue")}>
                <Icon name="queue" size={14} />排队
              </button>
            </div>
          </div>
        )}
        <div className="btw-input">
          <textarea
            ref={taRef}
            value={input}
            placeholder={p.opening ? "正在打开…"
              : runtimeBusy
                ? (primaryIsInterrupt
                  ? "可输入后打断并发送或排队…"
                  : "输入以引导当前任务，或选择排队…")
                : "问点什么"}
            rows={1}
            disabled={!!p.opening || !p.sid}
            onChange={(event) => {
              setInput(event.target.value);
              grow(event.target);
            }}
            onCompositionStart={() => imeSubmitRef.current.startComposition()}
            onCompositionEnd={(event) => {
              imeSubmitRef.current.endComposition();
              setInput(event.currentTarget.value);
            }}
            onPaste={(event) => {
              const text = event.clipboardData.getData("text/plain");
              if (text.length <= LONG_PASTE_THRESHOLD) return;
              event.preventDefault();
              updateDraft((current) => ({
                ...current,
                pastes: [
                  ...current.pastes,
                  makeComposerPaste(text, uuid()),
                ],
              }));
            }}
            onKeyDown={(event) => {
              if (!imeSubmitRef.current.shouldSubmitKey({
                key: event.key,
                shiftKey: event.shiftKey,
                isComposing: event.nativeEvent.isComposing,
                keyCode: event.nativeEvent.keyCode,
              })) return;
              event.preventDefault();
              submit(event.currentTarget.value);
            }}
          />
          <button className={sendClass}
            onPointerDown={() => {
              if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) {
                taRef.current?.blur();
              }
            }}
            onClick={requestButtonAction}
            disabled={sendDisabled}
            aria-label={interruptSettling && stopping
              ? "正在停止" : stopping ? "停止" : "发送"}>
            <Icon name={sendIcon} size={18} />
          </button>
        </div>
        <div className="btw-controls">
          <span>BTW 设置</span>
          <button className="hint-ctl" onClick={() => setSheetKind("models")}
            disabled={busy}>{model?.name ?? "模型读取中"}</button>
          <button className="hint-ctl" onClick={() => setSheetKind("efforts")}
            disabled={busy}>{effortName ?? "强度读取中"}</button>
        </div>
      </div>
      <CommandSheet
        open={sheetKind !== null}
        kind={sheetKind ?? "models"}
        engine={p.engine === "codex" ? "codex" : "claude"}
        catalog={p.catalog}
        currentModel={p.rt?.model}
        currentEffort={p.rt?.effort}
        onClose={() => setSheetKind(null)}
        onPickModel={(nextModel) => {
          if (!busy) p.onSetModel(nextModel);
          setSheetKind(null);
        }}
        onPickEffort={(nextEffort) => {
          if (!busy) p.onSetEffort(nextEffort);
          setSheetKind(null);
        }}
      />
      <>
        <div className={"scrim" + (autoCompactOpen ? " show" : "")}
          onClick={() => setAutoCompactOpen(false)} />
        <div className={"sheet auto-compact-sheet"
          + (autoCompactOpen ? " show" : "")}
          role="dialog" aria-label="BTW 自动压缩">
          <div className="sheet-grip" />
          <div className="sheet-title">BTW 自动压缩</div>
          <div className="sheet-scroll">
            <AutoCompactControl value={{
              mode: p.rt?.autoCompact?.mode ?? "inherit",
              thresholdTokens:
                p.rt?.autoCompact?.threshold_tokens ?? null,
            }} state={p.rt?.autoCompact}
              disabled={!p.sid}
              onChange={(selection) => {
                if (!p.onSetAutoCompact(selection)) {
                  flash("自动压缩设置暂未发送，请稍后重试。");
                }
              }} />
          </div>
        </div>
      </>
    </div>
  );
}
