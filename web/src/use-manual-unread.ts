import { useCallback, useEffect, useState } from "react";
import { MANUAL_UNREAD_STORAGE, readManualUnread, setManualUnread, type ManualUnreadScope } from "./manual-unread";

function browserStorage(): Storage | undefined {
  try {
    // Accessing the property itself can throw when browser storage is blocked.
    // Server rendering must not use Node's process-wide Web Storage either.
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

/** A local reading reminder, not an engine completion or a terminal receipt. */
export function useManualUnread() {
  const [marks, setMarks] = useState(() => {
    const storage = browserStorage();
    return storage ? readManualUnread(storage) : {};
  });
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      const storage = browserStorage();
      if (storage && event.storageArea === storage && (!event.key || event.key === MANUAL_UNREAD_STORAGE))
        setMarks(readManualUnread(storage));
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  const update = useCallback((scope: ManualUnreadScope, sid: string, unread: boolean) => {
    const next = setManualUnread(marks, scope, sid, unread);
    try { browserStorage()?.setItem(MANUAL_UNREAD_STORAGE, JSON.stringify(next)); } catch { /* Keep the in-memory reminder. */ }
    setMarks(next);
  }, [marks]);
  return { marks, update };
}
