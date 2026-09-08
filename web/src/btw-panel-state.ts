import type { Engine, Space } from "./protocol";

export const BTW_PANEL_SCOPES_KEY = "cc-remote:btw-panel-scopes-v1";
const MAX_OPEN_PANELS = 64;
const MAX_KEY_LENGTH = 2048;

export function btwPanelScopeKey(
  machineId: string, space: Space, engine: Engine, parentSid: string,
): string {
  return JSON.stringify([machineId, space, engine, parentSid]);
}

/** Visibility is tab-local UI state, never authority to create/close a fork.
 * The old global boolean has no parent identity and cannot be safely restored.
 * Its retained chats still come from the server's independent BTW catalog. */
export function readBtwPanelScopes(
  storage: Pick<Storage, "getItem">,
): string[] {
  try {
    const raw = storage.getItem(BTW_PANEL_SCOPES_KEY);
    if (!raw || raw.length > MAX_OPEN_PANELS * MAX_KEY_LENGTH * 2) return [];
    const keys: unknown = JSON.parse(raw);
    if (!Array.isArray(keys)) return [];
    return [...new Set(keys.filter((key): key is string => {
      if (typeof key !== "string" || key.length > MAX_KEY_LENGTH) return false;
      try {
        const scope: unknown = JSON.parse(key);
        return Array.isArray(scope) && scope.length === 4
          && typeof scope[0] === "string" && !!scope[0]
          && (scope[1] === "code" || scope[1] === "work")
          && (scope[2] === "codex" || scope[2] === "claude")
          && typeof scope[3] === "string" && !!scope[3];
      } catch { return false; }
    }))].slice(-MAX_OPEN_PANELS);
  } catch { return []; }
}

export function setBtwPanelScope(
  scopes: string[], key: string, visible: boolean,
): string[] {
  const next = scopes.filter((scope) => scope !== key);
  if (visible && key.length <= MAX_KEY_LENGTH) next.push(key);
  return next.slice(-MAX_OPEN_PANELS);
}

export function rekeyBtwPanelScope(
  scopes: string[], oldKey: string, newKey: string,
): string[] {
  return scopes.includes(oldKey)
    ? setBtwPanelScope(scopes.filter((key) => key !== oldKey), newKey, true)
    : scopes;
}
