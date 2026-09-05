import type { RateLimitResetOutcome, StatusRateLimitResetCredit } from "./protocol";

type ResetCreditText = Pick<StatusRateLimitResetCredit, "title" | "description">;

// Translate known upstream copy only. Preserve unfamiliar terms/conditions
// verbatim instead of implying that a different credit has the same scope.
const RESET_CREDIT_COPY = new Map([
  ["Full reset", "完整额度重置券"],
  ["Thanks for using Codex! You've been granted one free rate limit reset.",
    "感谢使用 Codex！你已获赠一次免费的额度重置机会。"],
]);

export function resetCreditPresentation(credit?: ResetCreditText) {
  const title = credit?.title?.trim() || "Codex 额度重置券";
  const description = credit?.description?.trim() || "";
  return {
    title: RESET_CREDIT_COPY.get(title) ?? title,
    description: RESET_CREDIT_COPY.get(description) ?? description,
  };
}

export function resetCreditConfirmation(credit?: ResetCreditText): string {
  const copy = resetCreditPresentation(credit);
  const name = credit?.title?.trim() ? copy.title : "一张 Codex 额度重置券";
  const detail = copy.description ? `\n${copy.description}` : "";
  return `确定使用${name}吗？${detail}\n\n这会消耗一张重置券，并重置当前符合条件的额度窗口。`;
}

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
