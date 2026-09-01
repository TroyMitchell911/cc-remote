import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";
import { resolveBrowserAddress } from "../browser-address";
import { Icon } from "../icons";
import type {
  BrowserActionName,
  BrowserFrame,
  BrowserSurface,
  ErrorMsg,
} from "../protocol";
import type { RelayWs } from "../ws";
import { PanelResizer } from "./PanelResizer";
import { PanelTabs, type RightPanelView } from "./PanelTabs";

type BrowserEvent = BrowserSurface | BrowserFrame | ErrorMsg;

const SURFACE_POLL_MS = 500;
const FRAME_STALE_MS = 4_000;
const OWNED_REQUEST_CAP = 64;
const ACTION_QUEUE_CAP = 128;
const URL_MAX_CHARS = 8_192;
const TEXT_MAX_CHARS = 64 * 1024;

type BrowserRequestKind = "surface" | "frame" | "action";
type OwnedBrowserRequest = { sequence: number; kind: BrowserRequestKind };
type QueuedBrowserAction = {
  action: BrowserActionName;
  args: Record<string, string | number>;
  onSettled?: (success: boolean) => void;
};

export function BrowserPanel({
  sid,
  ws,
  online,
  active,
  hasArtifact,
  hasBtw,
  onTab,
  onListen,
  onRequest,
  onClose,
}: {
  sid: string;
  ws: RelayWs | null;
  online: boolean;
  active: RightPanelView;
  hasArtifact: boolean;
  hasBtw: boolean;
  onTab: (view: RightPanelView) => void;
  onListen: (listener: ((event: BrowserEvent) => boolean) | null) => void;
  onRequest: (requestId: string) => void;
  onClose: () => void;
}) {
  const [surface, setSurface] = useState<BrowserSurface | null>(null);
  const [frame, setFrame] = useState<BrowserFrame | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [textDraft, setTextDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [actionPending, setActionPending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const ownedRequests = useRef(new Map<string, OwnedBrowserRequest>());
  const requestSequence = useRef(0);
  const lastSurfaceSequence = useRef(0);
  const lastFrameSequence = useRef(0);
  const actionRequests = useRef(
    new Map<string, QueuedBrowserAction | null>(),
  );
  const queuedActions = useRef<QueuedBrowserAction[]>([]);
  const actionPendingRef = useRef(false);
  const navigationPendingRef = useRef(false);
  const pendingFrame = useRef<{ id: string; at: number } | null>(null);
  const frameRef = useRef<BrowserFrame | null>(null);
  const surfaceKnownRef = useRef(false);
  const generationRef = useRef<string | null>(null);
  const controlledRef = useRef(false);
  const onlineRef = useRef(online);
  const urlEditingRef = useRef(false);
  const lastWheelAt = useRef(0);
  const noticeTimer = useRef<number | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  generationRef.current = surface?.generation ?? frame?.generation ?? null;
  controlledRef.current = surface?.controlled_by_me
    ?? frame?.controlled_by_me
    ?? false;
  onlineRef.current = online;
  frameRef.current = frame;

  const own = useCallback((
    requestId: string | null, kind: BrowserRequestKind,
  ) => {
    if (!requestId) return null;
    ownedRequests.current.set(requestId, {
      sequence: ++requestSequence.current,
      kind,
    });
    onRequest(requestId);
    while (ownedRequests.current.size > OWNED_REQUEST_CAP) {
      const oldest = [...ownedRequests.current.keys()].find((candidate) => (
        candidate !== pendingFrame.current?.id
        && !actionRequests.current.has(candidate)
      ));
      if (!oldest) break;
      ownedRequests.current.delete(oldest);
    }
    return requestId;
  }, [onRequest]);

  const requestFrame = useCallback(() => {
    if (!ws || !onlineRef.current) return false;
    const pending = pendingFrame.current;
    if (pending && Date.now() - pending.at < FRAME_STALE_MS) return false;
    const requestId = own(ws.sendGetBrowserFrameTo(
      sid, generationRef.current), "frame");
    if (!requestId) return false;
    pendingFrame.current = { id: requestId, at: Date.now() };
    return true;
  }, [own, sid, ws]);

  const refreshSurface = useCallback((
    create: boolean, showLoading = true,
  ) => {
    if (!ws || !onlineRef.current) return false;
    const requestId = own(
      ws.sendGetBrowserSurfaceTo(sid, create), "surface");
    if (!requestId) return false;
    if (showLoading) setLoading(true);
    return true;
  }, [own, sid, ws]);

  const clearNoticeTimer = useCallback(() => {
    if (noticeTimer.current !== null) {
      window.clearTimeout(noticeTimer.current);
      noticeTimer.current = null;
    }
  }, []);

  const showNotice = useCallback((message: string | null) => {
    clearNoticeTimer();
    setNotice(message);
    if (message) {
      noticeTimer.current = window.setTimeout(() => {
        noticeTimer.current = null;
        setNotice(null);
      }, 5_000);
    }
  }, [clearNoticeTimer]);

  const rejectQueuedActions = useCallback(() => {
    const rejected = queuedActions.current.splice(0);
    for (const queued of rejected) queued.onSettled?.(false);
  }, []);

  const dispatchAction = useCallback((queued: QueuedBrowserAction) => {
    const generation = generationRef.current;
    if (!ws || !onlineRef.current || !generation || !controlledRef.current
        || actionPendingRef.current) return false;
    const requestId = own(ws.sendBrowserActionTo(
      sid, generation, queued.action, queued.args), "action");
    if (!requestId) return false;
    actionRequests.current.set(requestId, queued);
    actionPendingRef.current = true;
    setActionPending(true);
    return true;
  }, [own, sid, ws]);

  const drainActionQueue = useCallback(() => {
    if (actionPendingRef.current) return;
    const queued = queuedActions.current.shift();
    if (!queued) return;
    if (dispatchAction(queued)) return;
    queued.onSettled?.(false);
    rejectQueuedActions();
    showNotice("浏览器控制已失效，未执行剩余操作");
  }, [dispatchAction, rejectQueuedActions, showNotice]);

  const sendAction = useCallback((
    action: BrowserActionName,
    args: Record<string, string | number>,
    onSettled?: (success: boolean) => void,
  ) => {
    if (!ws || !onlineRef.current || !generationRef.current
        || !controlledRef.current) return false;
    const queued: QueuedBrowserAction = { action, args, onSettled };
    if (!actionPendingRef.current) return dispatchAction(queued);

    const tail = queuedActions.current.at(-1);
    if (
      action === "type"
      && onSettled === undefined
      && tail?.action === "type"
      && tail.onSettled === undefined
      && typeof tail.args.text === "string"
      && typeof args.text === "string"
      && tail.args.text.length + args.text.length <= TEXT_MAX_CHARS
    ) {
      tail.args = { text: tail.args.text + args.text };
      return true;
    }
    if (queuedActions.current.length >= ACTION_QUEUE_CAP) {
      showNotice("浏览器输入过快，请稍后继续");
      return false;
    }
    queuedActions.current.push(queued);
    return true;
  }, [dispatchAction, showNotice, ws]);

  useEffect(() => {
    const requests = ownedRequests.current;
    const actions = actionRequests.current;
    const listener = (event: BrowserEvent) => {
      const requestId = event.request_id;
      const owned = typeof requestId === "string"
        ? requests.get(requestId) : undefined;
      if (
        event.sid !== sid
        || typeof requestId !== "string"
        || owned === undefined
      ) {
        return false;
      }
      requests.delete(requestId);
      const actionResponse = actions.has(requestId);
      const settledAction = actions.get(requestId);
      actions.delete(requestId);
      if (actionResponse) {
        actionPendingRef.current = false;
        setActionPending(false);
        if (navigationPendingRef.current) {
          navigationPendingRef.current = false;
          urlEditingRef.current = false;
          if (event.type === "browser_surface" && event.url) {
            setUrlDraft(event.url);
          }
        }
      }
      const settleAction = (success: boolean) => {
        if (!actionResponse) return;
        settledAction?.onSettled?.(success);
        if (!success) {
          rejectQueuedActions();
          return;
        }
        drainActionQueue();
      };
      if (event.type === "error") {
        if (pendingFrame.current?.id === requestId) {
          pendingFrame.current = null;
        }
        showNotice(event.message);
        setLoading(false);
        settleAction(false);
        return true;
      }
      if (event.type === "browser_surface") {
        // A status poll issued after an action may finish before that action.
        // Always settle the matching action/error first, then use request order
        // only to prevent older projection reads from replacing newer ones.
        if (owned.kind === "action") {
          showNotice(event.error ?? null);
        } else if (event.error) {
          showNotice(event.error);
        }
        const refreshAfterAction = actionResponse && !event.error
          && event.available && event.enabled && !!event.generation;
        if (owned.sequence < lastSurfaceSequence.current) {
          // A lightweight status poll may overtake a slower navigation/click.
          // It may replace projection metadata, but it cannot erase the one
          // viewport pull owed by the completed user action.
          if (refreshAfterAction) {
            window.setTimeout(() => requestFrame(), 0);
          }
          settleAction(!event.error);
          return true;
        }
        lastSurfaceSequence.current = owned.sequence;
        const needsFrame = !!event.generation && (
          frameRef.current?.generation !== event.generation
          || !frameRef.current?.data
          || event.frame_revision > (frameRef.current?.frame_revision ?? -1)
        );
        if (!event.generation || (
          frameRef.current?.generation
          && frameRef.current.generation !== event.generation
        )) {
          // Never leave a clickable screenshot from a retired page attached to
          // a missing/new generation while its replacement is in flight.
          frameRef.current = null;
          setFrame(null);
        }
        generationRef.current = event.generation ?? null;
        controlledRef.current = event.controlled_by_me;
        surfaceKnownRef.current = true;
        setSurface(event);
        setLoading(false);
        if (event.url && !urlEditingRef.current) setUrlDraft(event.url);
        if (event.available && event.enabled && needsFrame) {
          window.setTimeout(() => requestFrame(), 0);
        } else if (
          refreshAfterAction
        ) {
          // User actions do not capture on the wrapper. Pull the resulting
          // viewport immediately instead of waiting for a periodic JPEG poll.
          window.setTimeout(() => requestFrame(), 0);
        }
        settleAction(!event.error);
        return true;
      }
      if (pendingFrame.current?.id === requestId) {
        pendingFrame.current = null;
      }
      if (owned.sequence < lastFrameSequence.current) return true;
      lastFrameSequence.current = owned.sequence;
      if (event.error) {
        showNotice(event.error);
        if (!frameRef.current?.data) setFrame(event);
        setLoading(false);
        return true;
      }
      const activeGeneration = generationRef.current;
      if (surfaceKnownRef.current && !activeGeneration) {
        refreshSurface(false, false);
        return true;
      }
      if (activeGeneration && event.generation !== activeGeneration) {
        refreshSurface(false, false);
        return true;
      }
      const currentFrame = frameRef.current;
      if (
        currentFrame !== null
        && currentFrame.generation === event.generation
        && currentFrame.frame_revision > event.frame_revision
      ) {
        return true;
      }
      setFrame(event);
      generationRef.current = event.generation ?? null;
      controlledRef.current = event.controlled_by_me;
      setSurface((current) => current ? {
        ...current,
        surface_id: event.surface_id,
        generation: event.generation,
        frame_revision: event.frame_revision,
        width: event.width,
        height: event.height,
        url: event.url,
        title: event.title,
        control_mode: event.control_mode,
        controlled_by_me: event.controlled_by_me,
        error: null,
      } : current);
      if (event.url && !urlEditingRef.current) setUrlDraft(event.url);
      setLoading(false);
      return true;
    };
    onListen(listener);
    return () => {
      onListen(null);
      requests.clear();
      actions.clear();
      queuedActions.current = [];
      actionPendingRef.current = false;
      navigationPendingRef.current = false;
      pendingFrame.current = null;
      surfaceKnownRef.current = false;
    };
  }, [
    drainActionQueue,
    onListen,
    refreshSurface,
    rejectQueuedActions,
    requestFrame,
    showNotice,
    sid,
  ]);

  useEffect(() => {
    if (online) refreshSurface(true);
  }, [online, refreshSurface]);

  useEffect(() => clearNoticeTimer, [clearNoticeTimer]);

  const releaseControlIfHeld = useCallback(() => {
    if (!controlledRef.current) return;
    controlledRef.current = false;
    const requestId = ws?.sendReleaseBrowserControlTo(sid);
    if (requestId) onRequest(requestId);
  }, [onRequest, sid, ws]);

  useEffect(() => releaseControlIfHeld, [releaseControlIfHeld]);

  const shouldPollSurface = surface !== null
    && surface.available
    && surface.enabled;
  useEffect(() => {
    if (!online || !shouldPollSurface) return;
    const timer = window.setInterval(
      () => refreshSurface(false, false), SURFACE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [online, refreshSurface, shouldPollSurface]);

  const toggleControl = () => {
    if (!ws || !online || actionPendingRef.current) return;
    const requestId = controlledRef.current
      ? ws.sendReleaseBrowserControlTo(sid)
      : ws.sendAcquireBrowserControlTo(sid);
    const owned = own(requestId, "action");
    if (owned) {
      actionRequests.current.set(owned, null);
      actionPendingRef.current = true;
      setActionPending(true);
    }
  };

  const navigateFromAddress = useCallback(() => {
    const resolved = resolveBrowserAddress(urlDraft);
    if (!resolved.ok) {
      showNotice(resolved.message);
      return;
    }
    if (sendAction("navigate", { url: resolved.url })) {
      navigationPendingRef.current = true;
      urlEditingRef.current = true;
      setUrlDraft(resolved.url);
    }
  }, [sendAction, showNotice, urlDraft]);

  const close = () => {
    releaseControlIfHeld();
    onClose();
  };

  const clickFrame = (event: ReactMouseEvent<HTMLImageElement>) => {
    if (!controlledRef.current) return;
    stageRef.current?.focus();
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) return;
    const width = frame?.width ?? surface?.width ?? 1280;
    const height = frame?.height ?? surface?.height ?? 800;
    const x = Math.max(0, Math.min(
      width - 1, Math.round((event.clientX - bounds.left) * width / bounds.width)));
    const y = Math.max(0, Math.min(
      height - 1, Math.round((event.clientY - bounds.top) * height / bounds.height)));
    sendAction("click", { x, y, button: "left" });
  };

  const wheelFrame = (event: ReactWheelEvent<HTMLImageElement>) => {
    if (!controlledRef.current || Date.now() - lastWheelAt.current < 120) return;
    event.preventDefault();
    lastWheelAt.current = Date.now();
    const deltaX = Math.max(-5000, Math.min(5000, Math.round(event.deltaX)));
    const deltaY = Math.max(-5000, Math.min(5000, Math.round(event.deltaY)));
    if (deltaX || deltaY) sendAction("scroll", {
      delta_x: deltaX,
      delta_y: deltaY,
    });
  };

  const keyFrame = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!controlledRef.current || event.target instanceof HTMLInputElement) return;
    const modifiers = [
      event.altKey ? "Alt" : "",
      event.ctrlKey ? "Control" : "",
      event.metaKey ? "Meta" : "",
      event.shiftKey ? "Shift" : "",
    ].filter(Boolean);
    const named = new Set([
      "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowUp", "Backspace",
      "Delete", "End", "Enter", "Escape", "Home", "Insert", "PageDown",
      "PageUp", "Tab",
    ]);
    if (event.key.length === 1 && modifiers.length === 0) {
      event.preventDefault();
      sendAction("type", { text: event.key });
      return;
    }
    const key = event.key === " " ? "Space" : event.key;
    if (!named.has(key) && !/^F(?:[1-9]|1[0-2])$/.test(key)
        && !(key.length === 1 && modifiers.length > 0)) return;
    event.preventDefault();
    sendAction("press", { key: [...modifiers, key].join("+") });
  };

  const enabled = surface?.enabled !== false;
  const available = surface?.available !== false;
  const controlled = surface?.controlled_by_me
    ?? frame?.controlled_by_me
    ?? false;
  const heldByOther = surface?.control_mode === "user" && !controlled;
  const controlsDisabled = !online || !available || !enabled || !controlled
    || actionPending || !generationRef.current;
  const error = notice ?? surface?.error ?? frame?.error ?? null;
  const image = frame?.data && !frame.error
    ? `data:image/jpeg;base64,${frame.data}` : null;
  // Surface polling is the authoritative current ownership state. A frame is
  // an immutable capture and can legitimately retain `agent` after the tool
  // call that produced it has already completed.
  const agentActive = (surface?.control_mode ?? frame?.control_mode) === "agent";
  const status = controlled ? "你正在浏览"
    : heldByOther ? "其他设备正在浏览"
      : agentActive ? "Codex 正在浏览"
        : surface?.agent_available ? "共享浏览器" : "手动浏览";
  const statusTone = controlled ? "user"
    : heldByOther ? "other" : agentActive ? "agent" : "ready";
  const pageTitle = surface?.title || frame?.title || "新标签页";
  let pageHost = "独立 Profile";
  try {
    const currentUrl = surface?.url || frame?.url;
    if (currentUrl) pageHost = new URL(currentUrl).hostname || pageHost;
  } catch {
    // Keep the privacy label for an empty or transient browser URL.
  }
  const watchMessage = controlled
    ? "你已接管页面；交还后 Codex 可继续操作"
    : heldByOther ? "另一台设备正在操作这个页面"
      : agentActive ? "正在实时同步 Codex 的浏览步骤"
        : surface?.agent_available
          ? "浏览器已与当前 Codex 会话共享"
          : "当前会话只支持手动浏览";

  return (
    <div className="browser-panel" data-lock-horizontal-swipe="true">
      <PanelResizer ariaLabel="调整浏览器面板宽度" />
      <div className="browser-head">
        {(hasArtifact || hasBtw)
          ? <PanelTabs active={active} hasArtifact={hasArtifact}
              hasBtw={hasBtw} hasBrowser onTab={onTab} />
          : <div className="browser-title"><Icon name="globe" size={15} /> 浏览器</div>}
        <span className={`browser-control-state ${statusTone}`}>
          <span className="browser-live-dot" />{status}
        </span>
        <button type="button" className="iconbtn" onClick={close}
          aria-label="关闭浏览器面板" title="关闭浏览器面板">
          <Icon name="chevrons-right" />
        </button>
      </div>
      <div className="browser-chrome">
        <div className="browser-tabbar">
          <div className="browser-tab" title={pageTitle}>
            <span className="browser-favicon"><Icon name="globe" size={13} /></span>
            <span className="browser-tab-title">{pageTitle}</span>
            {agentActive && <span className="browser-tab-pulse" aria-hidden="true" />}
          </div>
          <span className="browser-profile" title="与日常浏览器隔离的专用 Profile">
            <Icon name="shield" size={12} />{pageHost}
          </span>
        </div>
        <div className="browser-nav">
          <div className="browser-nav-history">
            <button type="button" onClick={() => sendAction("back", {})}
              disabled={controlsDisabled} title="后退" aria-label="后退">
              <Icon name="chevron-left" size={16} />
            </button>
            <button type="button" onClick={() => sendAction("forward", {})}
              disabled={controlsDisabled} title="前进" aria-label="前进">
              <Icon name="chevron-right" size={16} />
            </button>
            <button type="button" onClick={() => requestFrame()}
              disabled={!online || !available || !enabled} title="同步最新画面"
              aria-label="同步最新画面"><Icon name="refresh" size={15} /></button>
          </div>
          <form className="browser-address" onSubmit={(event) => {
            event.preventDefault();
            navigateFromAddress();
          }}>
            <Icon name="search" size={14} />
            <input aria-label="浏览器地址" value={urlDraft}
              maxLength={URL_MAX_CHARS}
              placeholder="搜索 Google 或输入网址"
              disabled={controlsDisabled}
              autoCapitalize="none" autoCorrect="off" spellCheck={false}
              onFocus={() => { urlEditingRef.current = true; }}
              onBlur={() => { urlEditingRef.current = false; }}
              onChange={(event) => {
                urlEditingRef.current = true;
                setUrlDraft(event.target.value);
              }} />
            <button type="submit" className="browser-go"
              disabled={controlsDisabled || !urlDraft.trim()}
              aria-label="前往"><Icon name="send" size={14} /></button>
          </form>
          <button type="button"
            className={`browser-control-btn ${controlled ? "release" : "take"}`}
            onClick={toggleControl}
            disabled={!online || !available || !enabled || heldByOther || actionPending}>
            {controlled ? "交还" : "接管"}
          </button>
        </div>
      </div>
      <div className="browser-stage-shell">
        <div ref={stageRef} className="browser-stage" onKeyDown={keyFrame}
          tabIndex={controlled ? 0 : -1}>
          {image
            ? <img src={image} alt={pageTitle || "托管浏览器画面"}
                draggable={false} onClick={clickFrame} onWheel={wheelFrame}
                className={controlled ? "interactive" : ""} />
            : <div className="browser-empty" role="status">
                <span className="browser-empty-mark"><Icon name="globe" size={27} /></span>
                <strong>{loading ? "正在打开共享浏览器" : "浏览器画面暂不可用"}</strong>
                <span>{loading ? "正在连接 wrapper 本机的独立 Profile…"
                  : error || "请稍后重试或检查 Wrapper 浏览器配置"}</span>
              </div>}
          {image && <div className={`browser-stage-status ${statusTone}`}>
            <span className="browser-live-dot" />{status}
          </div>}
          {actionPending && <div className="browser-action-pending"
            aria-label="正在执行浏览器操作"><span className="spinner" /></div>}
          {notice && image && <div className="browser-notice" role="alert">
            {notice}
          </div>}
        </div>
      </div>
      <div className={`browser-footer ${controlled ? "controlled" : "watching"}`}>
        {controlled
          ? <>
              <div className="browser-inputbar">
                <span className="browser-input-prefix" aria-hidden="true">›</span>
                <input aria-label="输入到当前网页" value={textDraft}
                  maxLength={TEXT_MAX_CHARS}
                  placeholder="输入到当前焦点"
                  disabled={controlsDisabled}
                  onChange={(event) => setTextDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" || !textDraft) return;
                    event.preventDefault();
                    const submitted = textDraft;
                    sendAction("type", { text: submitted }, (success) => {
                      if (success) {
                        setTextDraft((current) => (
                          current === submitted ? "" : current
                        ));
                      }
                    });
                  }} />
                <button type="button" className="browser-type-send"
                  disabled={controlsDisabled || !textDraft}
                  onClick={() => {
                    const submitted = textDraft;
                    sendAction("type", { text: submitted }, (success) => {
                      if (success) {
                        setTextDraft((current) => (
                          current === submitted ? "" : current
                        ));
                      }
                    });
                  }} aria-label="输入文本"><Icon name="send" size={15} /></button>
              </div>
              <div className="browser-key-controls">
                <button type="button" disabled={controlsDisabled}
                  onClick={() => sendAction("scroll", { delta_x: 0, delta_y: -640 })}
                  aria-label="向上滚动"><Icon name="chev" size={14} /></button>
                <button type="button" className="browser-enter-key"
                  disabled={controlsDisabled}
                  onClick={() => sendAction("press", { key: "Enter" })}>Enter</button>
                <button type="button" disabled={controlsDisabled}
                  onClick={() => sendAction("scroll", { delta_x: 0, delta_y: 640 })}
                  aria-label="向下滚动"><Icon name="chev" size={14} /></button>
              </div>
            </>
          : <div className={`browser-watch-message ${statusTone}`}>
              <span className="browser-watch-icon"><Icon
                name={agentActive ? "spark" : "eye"} size={15} /></span>
              <span><strong>{watchMessage}</strong>
                <small>{agentActive ? "画面会随每一步工具操作自动更新"
                  : "接管后可以点击、滚动和输入"}</small></span>
            </div>}
      </div>
    </div>
  );
}
