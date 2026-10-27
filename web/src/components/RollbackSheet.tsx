import { useState } from "react";
import type { RestoreMode, RewindPreview, RollbackResult } from "../protocol";
import { CenteredSheet } from "./CenteredSheet";
import "./RollbackSheet.css";

const formatTime = (ts: number) => new Date(ts).toLocaleString(undefined, {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});

export interface RollbackTarget {
  checkpointId: string;
  prompt: string;
  ts?: number;
}

export function RollbackSheet({ target, choices, preview, pending, result, error,
  disabledReason, onClose, onChoose, onConfirm, onLoadMore, prefillSkipped }: {
  target: RollbackTarget | null;
  choices: RollbackTarget[];
  preview: RewindPreview | null;
  pending: boolean;
  result: RollbackResult | null;
  error: string | null;
  disabledReason: string | null;
  prefillSkipped: boolean;
  onClose: () => void;
  onChoose: (target: RollbackTarget) => void;
  onConfirm: (mode: RestoreMode) => void;
  onLoadMore?: () => void;
}) {
  const [mode, setMode] = useState<RestoreMode>("both");
  const [expanded, setExpanded] = useState(false);
  const filesAvailable = preview?.files_available === true;
  const selectedMode = !filesAvailable && mode !== "conversation" ? "conversation" : mode;
  const prompt = preview?.prompt || target?.prompt || "（含附件的消息）";
  const block = disabledReason || error || (preview && !preview.available ? preview.detail : null);
  const options: { mode: RestoreMode; title: string; detail: string }[] = [
    { mode: "both", title: "恢复对话和文件", detail: "对话和文件一起恢复到这条消息之前" },
    { mode: "conversation", title: "仅恢复对话", detail: "保留当前文件，将原消息放回输入框" },
    { mode: "files", title: "仅恢复文件", detail: "恢复对应文件检查点，保留全部对话" },
  ];
  return <CenteredSheet open label="回退" onClose={() => { if (!pending) onClose(); }}
    className="rewind-sheet" maxWidth={560} maxHeight={780}>
    <div className="rewind-body">
      {!target ? <>
        <h3>选择要回到的消息</h3>
        <p className="rewind-help">回到发送所选消息之前。</p>
        <div className="rewind-choices">
          {[...choices].reverse().map(choice => <button key={choice.checkpointId}
            className="qa-opt" onClick={() => { setExpanded(false); onChoose(choice); }}>
            {choice.ts && <small>{formatTime(choice.ts)}</small>}
            <span>{choice.prompt || "（含附件的消息）"}</span>
          </button>)}
          {choices.length === 0 && <p>当前页没有可回退的消息。</p>}
          {onLoadMore && <button className="rewind-more" onClick={onLoadMore}>加载更早的消息</button>}
        </div>
      </> : <>
        <h3>回到发送这条消息之前</h3>
        <div className="rewind-prompt">
          {target.ts && <small>{formatTime(target.ts)}</small>}
          <div className={expanded ? "rewind-original expanded" : "rewind-original"}>{prompt}</div>
          <button onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
            {expanded ? "收起原文" : "展开原文"}
          </button>
        </div>
        <div className="rewind-options" role="radiogroup" aria-label="回退范围">
          {options.map(option => <button key={option.mode} type="button" role="radio"
            aria-checked={selectedMode === option.mode}
            disabled={pending || !!result || !preview?.available
              || (option.mode !== "conversation" && !filesAvailable)}
            className={`qa-opt${selectedMode === option.mode ? " selected" : ""}`}
            onClick={() => setMode(option.mode)}>
            <b>{option.title}</b><small>{option.detail}</small>
          </button>)}
        </div>
        {!preview && !error && <p role="status" className="rewind-help">正在检查回退点…</p>}
        {preview?.available && !filesAvailable && <p className="rewind-help">
          此处没有可用的文件检查点，可仅恢复对话。</p>}
        <p className="rewind-help">仅恢复 Claude 检查点记录的文件。<br />这些文件后续的手工修改也可能被覆盖。</p>
        {result && <div role="status" className="rewind-result">
          {result.restore !== "files" && <p>对话：{result.conversation === "succeeded" ? "已恢复" : "未恢复"}</p>}
          {result.restore !== "conversation" && <p>文件：{result.files === "succeeded" ? "已恢复" : "未恢复"}</p>}
          {result.detail && <p>{result.detail}</p>}
          {prefillSkipped && <p>已保留输入框里的草稿。原消息可从上方展开后复制。</p>}
        </div>}
      </>}
      {block && <p role="alert" className="rewind-error">{block}</p>}
    </div>
    <footer className="rewind-footer">
      <button className="rewind-cancel" disabled={pending} onClick={onClose}>{result ? "关闭" : "取消"}</button>
      {target && !result && <button className="rewind-confirm"
        disabled={pending || !!block || !preview?.available || !preview.latest_user_message_id}
        onClick={() => onConfirm(selectedMode)}>{pending ? "正在回退…" : "确认回退"}</button>}
    </footer>
  </CenteredSheet>;
}
