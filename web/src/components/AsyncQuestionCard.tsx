import { useId, useRef, useState, type ReactNode } from "react";
import type { AsyncQuestionSpec } from "../protocol";

/** Async messages are conversation content, not approval leases. Replies use
 * the scoped query/steer outbox; browser acceptance is not model receipt. */
export default function AsyncQuestionCard({ children, questions, onReply }: {
  children: ReactNode;
  questions: AsyncQuestionSpec[];
  onReply?: (prompt: string) => boolean;
}) {
  const id = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const [notice, setNotice] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const sendingRef = useRef(false);
  return <form className="async-question-card" ref={formRef}
    aria-label="助手的补充问题"
    onInput={() => {
      setNotice("");
      setSubmitted(false);
      sendingRef.current = false;
    }}
    onSubmit={(event) => {
      event.preventDefault();
      if (!onReply || sendingRef.current || !formRef.current) return;
      // Read DOM values after pointer/IME commit; textarea Enter is a newline.
      const data = new FormData(formRef.current);
      const answers = questions.flatMap((question, index) => {
        const answer = String(data.get(`text-${index}`) ?? "").trim()
          || String(data.get(`option-${index}`) ?? "").trim();
        return answer ? [`问题：${question.title}\n回答：${answer}`] : [];
      });
      if (!answers.length) {
        setNotice("请填写至少一个回答。");
        return;
      }
      sendingRef.current = true;
      const sent = onReply(`补充回答：\n\n${answers.join("\n\n")}`);
      sendingRef.current = sent;
      setSubmitted(sent);
      setNotice(sent
        ? "已提交，发送状态请查看会话中的消息。"
        : "暂时无法发送。回答已保留，请稍后重试。");
    }}>
    <div className="async-question-heading">助手想补充确认</div>
    <p className="async-question-hint">任务不会因此暂停，你可以随时补充回答。</p>
    {questions.map((question, index) => (
      <fieldset key={index} disabled={!onReply || submitted}>
        <legend>{question.title}</legend>
        {question.options?.map((option, optionIndex) => (
          <label className="async-question-option" key={optionIndex}>
            <input type="radio" name={`option-${index}`} value={option}
              defaultChecked={optionIndex === 0} />
            <span>{option}</span>
          </label>
        ))}
        <label className="async-question-text" htmlFor={`${id}-${index}`}>
          {question.options?.length ? "其他回答（填写后替代选项）" : "你的回答"}
        </label>
        <textarea id={`${id}-${index}`} name={`text-${index}`} rows={2}
          maxLength={8192} placeholder="输入补充信息…" />
      </fieldset>
    ))}
    <details className="async-question-source">
      <summary>原始提问</summary>
      {children}
    </details>
    <div className="async-question-actions">
      {submitted
        ? <button type="button" onClick={() => {
            sendingRef.current = false;
            setSubmitted(false);
            setNotice("");
          }}>继续补充</button>
        : <button type="submit" disabled={!onReply}>发送补充</button>}
      {!onReply && <span>当前会话暂不可写</span>}
    </div>
    {notice && <p className="async-question-hint" role="status">{notice}</p>}
  </form>;
}
