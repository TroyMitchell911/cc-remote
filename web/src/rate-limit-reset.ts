import type { RateLimitResetOutcome } from "./protocol";

export function resetCreditOutcomeMessage(
  outcome: RateLimitResetOutcome,
): string {
  switch (outcome) {
    case "reset":
      return "额度窗口已重置。";
    case "alreadyRedeemed":
      return "这次请求此前已经成功，额度窗口已重置。";
    case "nothingToReset":
      return "当前没有符合条件的额度窗口，重置券未消耗。";
    case "noCredit":
      return "当前账户没有可用重置券。";
    case "unknown":
      return "重置请求已处理，请刷新状态确认结果。";
  }
}
