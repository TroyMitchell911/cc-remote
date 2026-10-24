import { Icon } from "../icons";
import { presentHistoricalTurnProblem } from "../problem-presentation";

export default function TurnProblem({ message, continuing, hasVisibleContent = false }: {
  message: string;
  continuing: boolean;
  hasVisibleContent?: boolean;
}) {
  const problem = presentHistoricalTurnProblem(message, continuing);
  if (!problem) {
    // A failed request with no reply or process must not look unanswered.
    return hasVisibleContent ? null : <p className="turn-failure-status" role="status">
      {continuing ? "这次回复未完成，后续回复正在处理中。" : "回复未完成，请重试。"}
    </p>;
  }
  return <div className="turn-problem" role="status">
    <Icon name="info" size={18} />
    <div>
      <p>{problem}</p>
      {continuing && <p className="turn-problem-continuation">
        后续回复正在处理中，无需重复发送。
      </p>}
    </div>
  </div>;
}
