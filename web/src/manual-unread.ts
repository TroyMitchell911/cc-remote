import type { Engine, Space } from "./protocol";
import { MANUAL_UNREAD_STORAGE } from "./manual-unread-storage.ts";

export { MANUAL_UNREAD_STORAGE };
export type ManualUnreadMarks = Record<string, number>;
export interface ManualUnreadScope { machineId: string; engine: Engine; space: Space }
const LIMIT = 512;

export function manualUnreadKey(scope: ManualUnreadScope, sid: string): string {
  return JSON.stringify([scope.machineId, scope.space, scope.engine, sid]);
}

export function readManualUnread(storage: Pick<Storage, "getItem">): ManualUnreadMarks {
  try {
    const raw = storage.getItem(MANUAL_UNREAD_STORAGE);
    if (!raw || raw.length > 256 * 1024) return {};
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([key, timestamp]) => {
      try {
        const parts: unknown = JSON.parse(key);
        return Array.isArray(parts) && parts.length === 4
          && parts.every((part) => typeof part === "string" && part.length > 0 && part.length <= 128)
          && ["code", "work"].includes(parts[1]) && ["claude", "codex"].includes(parts[2])
          && typeof timestamp === "number" && Number.isSafeInteger(timestamp) && timestamp > 0;
      } catch { return false; }
    }).sort((a, b) => Number(a[1]) - Number(b[1])).slice(-LIMIT)) as ManualUnreadMarks;
  } catch { return {}; }
}

export function setManualUnread(current: ManualUnreadMarks, scope: ManualUnreadScope,
  sid: string, unread: boolean, now = Date.now()): ManualUnreadMarks {
  const key = manualUnreadKey(scope, sid);
  const rest = Object.entries(current).filter(([existing]) => existing !== key);
  return Object.fromEntries(unread ? [...rest.slice(1 - LIMIT), [key, now]] : rest);
}
