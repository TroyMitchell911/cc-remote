import { useState } from "react";
import { Composer } from "../../src/components/Composer";
import { MessageBlock } from "../../src/components/MessageBlock";
import { ComposerDraftStore } from "../../src/composer-drafts";
import { PROTOCOL_VERSION, type StatusRateLimit, type StatusReport } from "../../src/protocol";

export function QuotaLayoutFixture() {
  const [drafts] = useState(() => new ComposerDraftStore());
  const [periods, setPeriods] = useState("5h,week");
  const limit: StatusRateLimit = {
    limit_id: "claude",
    primary: periods.includes("5h")
      ? { used_percent: 28, window_duration_mins: 300, resets_at: 1_900_000_000 } : null,
    secondary: periods.includes("week")
      ? { used_percent: 42, window_duration_mins: 10_080, resets_at: 1_901_000_000 } : null,
    monthly: periods.includes("month")
      ? { used_percent: 16, resets_at: 1_902_000_000 } : null,
  };
  const report: StatusReport = {
    type: "status_report", v: PROTOCOL_VERSION, ts: 1,
    thread: {}, runtime: {}, context: {}, rate_limits: [limit],
    quota_query_state: "enabled", component_errors: [],
  };
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh", minWidth: 0 }}>
    <nav style={{ display: "flex", gap: 12, padding: 12, flexWrap: "wrap" }}>
      {["5h,week", "5h,month", "5h,week,month", "month"].map(value =>
        <button key={value} onClick={() => setPeriods(value)}>{value}</button>)}
    </nav>
    <div data-testid="quota-prose" style={{ flex: 1, minHeight: 0, padding: 16, overflowY: "auto" }}>
      <MessageBlock done text={"验证结果\n\nMac、Linux 全部 **14/14 通过**。完整指纹集合 SHA-256 均为 `e36173385f67f3586ab271389508f9e99062315d3a6fc2c6fd1234567890abcdef01`。\n\n普通长文本也保留完整内容：abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrstuvwxyz0123456789。"} />
    </div>
    <Composer draftKey="quota-layout" draftStore={drafts}
      state="idle" connState="connected" wrapperOnline
      queue={[]} pendingSend={null} failedDeferred={[]}
      unconfirmedQueued={[]} unconfirmedReplaceable={[]}
      queueCapacity={{}} replaceQueueCapacity={{}}
      model="opus[1m]" effort="xhigh" perm="bypassPermissions"
      permissionProfile={null} permissionProfiles={null}
      webSearch={null} collaborationMode="default" engine="claude"
      editPrompt={null} onEditConsumed={() => {}}
      onSendQuery={() => false} onSteerQuery={() => false} onInterrupt={() => {}}
      onEnqueue={() => false} onSetPending={() => false}
      onRemoveQueued={() => {}} onInspectQueued={() => {}}
      onSetModel={() => {}} onSetEffort={() => {}} onSetPerm={() => {}}
      onSetPermissionProfile={() => {}} onGetPermissionProfiles={() => {}}
      onSetWebSearch={() => {}} onSetCollaborationMode={() => {}}
      onClear={() => {}} onContext={() => {}} onRefreshUsage={() => {}}
      contextReport={null} statusReport={report} rateLimits={[limit]} />
  </main>;
}
