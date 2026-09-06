import { useEffect, useRef, useState } from "react";
import type { PreviewAuthorizationState } from "../reducer";

export function PreviewAuthorizationPrompt({
  authorization,
  compact = false,
  onDecision,
}: {
  authorization: PreviewAuthorizationState;
  compact?: boolean;
  onDecision?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
}) {
  const attempted = useRef<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const key = JSON.stringify([authorization.requestId, authorization.authorizationId]);
  useEffect(() => {
    if (authorization.status !== "required" || !onDecision || attempted.current === key) return;
    attempted.current = key;
    // Opening a preview is the read intent. Reuse the exact-file, client/session
    // bound handshake; never grant a directory or request write permission.
    if (!onDecision(authorization, "allow")) setFailed(key);
  }, [authorization, key, onDecision]);
  return <span className={
    `preview-authorization${compact ? " compact" : ""}`
  } role="status">
    {failed === key || !onDecision
      ? "读取未完成，请重新打开文件重试。"
      : <><span className="thinking"><span/><span/><span/></span> 正在读取文件…</>}
  </span>;
}
