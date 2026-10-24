import { useEffect, useState } from "react";
import { Icon } from "../icons";

/** Brief native resyncs only change the header status, never the composer. */
export default function DshConnectionNotice({ error }: { error: string | null }) {
  const [delayed, setDelayed] = useState(false);
  const disconnected = !!error;
  useEffect(() => {
    if (!disconnected) return;
    const timer = window.setTimeout(() => setDelayed(true), 8_000);
    return () => { window.clearTimeout(timer); setDelayed(false); };
  }, [disconnected]);
  if (!disconnected || !delayed) return null;
  return <div className="notice-bar attention" role="status" aria-live="polite">
    <span className="notice-icon" aria-hidden="true"><Icon name="info" size={16} /></span>
    <span className="notice-copy"><strong>DSH 连接尚未恢复，正在自动重连。</strong><br />{error}</span>
  </div>;
}
