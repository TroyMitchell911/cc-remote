import { useCallback, useEffect, useState } from "react";
import { MANUAL_UNREAD_STORAGE, readManualUnread, setManualUnread, type ManualUnreadScope } from "./manual-unread";

/** A local reading reminder, not an engine completion or a terminal receipt. */
export function useManualUnread() {
  const [marks, setMarks] = useState(() => readManualUnread(localStorage));
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.storageArea === localStorage && (!event.key || event.key === MANUAL_UNREAD_STORAGE))
        setMarks(readManualUnread(localStorage));
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  const update = useCallback((scope: ManualUnreadScope, sid: string, unread: boolean) => {
    const next = setManualUnread(marks, scope, sid, unread);
    try { localStorage.setItem(MANUAL_UNREAD_STORAGE, JSON.stringify(next)); } catch { /* Keep the in-memory reminder. */ }
    setMarks(next);
  }, [marks]);
  return { marks, update };
}
