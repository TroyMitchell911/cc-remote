import { useEffect, useState } from "react";
import type { AutoCompact } from "../protocol";
import {
  AUTO_COMPACT_PRESETS,
  autoCompactSelectionLabel,
  formatAutoCompactTokens,
  normalizeAutoCompactSelection,
  validAutoCompactThreshold,
  type AutoCompactSelection,
} from "../auto-compact";

interface Props {
  value: AutoCompactSelection;
  state?: AutoCompact | null;
  effectiveThresholdTokens?: number | null;
  rawMaxTokens?: number | null;
  newSession?: boolean;
  disabled?: boolean;
  onChange: (selection: AutoCompactSelection) => void;
}

const sameSelection = (
  left: AutoCompactSelection,
  right: AutoCompactSelection,
): boolean => left.mode === right.mode
  && left.thresholdTokens === right.thresholdTokens;

export function AutoCompactControl({
  value,
  state,
  effectiveThresholdTokens,
  rawMaxTokens,
  newSession = false,
  disabled = false,
  onChange,
}: Props) {
  const normalized = normalizeAutoCompactSelection(
    value.mode, value.thresholdTokens);
  const customPreset = normalized.mode === "custom"
    && AUTO_COMPACT_PRESETS.includes(
      normalized.thresholdTokens as typeof AUTO_COMPACT_PRESETS[number]);
  const [customOpen, setCustomOpen] = useState(
    normalized.mode === "custom" && !customPreset);
  const [customThousands, setCustomThousands] = useState(
    normalized.mode === "custom" && normalized.thresholdTokens !== null
      ? String(normalized.thresholdTokens / 1_000)
      : "",
  );

  useEffect(() => {
    if (normalized.mode !== "custom" || normalized.thresholdTokens === null) {
      return;
    }
    setCustomThousands(String(normalized.thresholdTokens / 1_000));
    setCustomOpen(!AUTO_COMPACT_PRESETS.includes(
      normalized.thresholdTokens as typeof AUTO_COMPACT_PRESETS[number]));
  }, [normalized.mode, normalized.thresholdTokens]);

  const readOnly = state != null && !state.mutable;
  const locked = disabled || readOnly;
  const applied = state?.applied_mode
    ? normalizeAutoCompactSelection(
      state.applied_mode, state.applied_threshold_tokens)
    : null;
  const customTokens = Number(customThousands) * 1_000;
  const customValid = validAutoCompactThreshold(customTokens);

  const choose = (selection: AutoCompactSelection) => {
    setCustomOpen(false);
    if (!sameSelection(normalized, selection)) onChange(selection);
  };

  return (
    <div className="auto-compact-control">
      <div className="auto-compact-head">
        <span>自动压缩</span>
        <b>{state?.pending ? "等待安全边界"
          : autoCompactSelectionLabel(normalized)}</b>
      </div>
      <div className="auto-compact-options" role="group"
        aria-label="Claude 自动压缩阈值">
        <button type="button"
          className={normalized.mode === "inherit" ? "selected" : ""}
          disabled={locked}
          onClick={() => choose({ mode: "inherit", thresholdTokens: null })}>
          跟随 Claude
        </button>
        <button type="button"
          className={normalized.mode === "auto" ? "selected" : ""}
          disabled={locked}
          onClick={() => choose({ mode: "auto", thresholdTokens: null })}>
          自动
        </button>
        {AUTO_COMPACT_PRESETS.map((thresholdTokens) => (
          <button type="button" key={thresholdTokens}
            className={!customOpen && normalized.mode === "custom"
              && normalized.thresholdTokens === thresholdTokens
              ? "selected" : ""}
            disabled={locked}
            onClick={() => choose({ mode: "custom", thresholdTokens })}>
            {formatAutoCompactTokens(thresholdTokens)}
          </button>
        ))}
        <button type="button"
          className={customOpen ? "selected" : ""}
          disabled={locked}
          onClick={() => setCustomOpen(true)}>
          自定义
        </button>
      </div>
      {customOpen && (
        <div className="auto-compact-custom">
          <label>
            <input type="number" min="100" max="1000" step="10"
              value={customThousands}
              disabled={locked}
              onChange={(event) => setCustomThousands(event.target.value)} />
            <span>K tokens</span>
          </label>
          <button type="button" disabled={locked || !customValid}
            onClick={() => choose({
              mode: "custom", thresholdTokens: customTokens,
            })}>应用</button>
        </div>
      )}
      <div className="auto-compact-meta">
        {state?.pending && applied && (
          <span>当前仍为 {autoCompactSelectionLabel(applied)}；将在下一次可确认的回合终态或下一条消息前切换。</span>
        )}
        {readOnly && (
          <span>本机 Claude TUI 正在控制此会话；这里只展示启动参数。</span>
        )}
        {state?.error && <span className="error">{state.error}</span>}
        {effectiveThresholdTokens != null && effectiveThresholdTokens > 0 && (
          <span>实际阈值 {formatAutoCompactTokens(effectiveThresholdTokens)}
            {rawMaxTokens != null && rawMaxTokens > 0
              ? ` · 原始窗口 ${formatAutoCompactTokens(rawMaxTokens)}` : ""}
          </span>
        )}
        {newSession && (
          <span>随新会话首次启动生效，不会额外重连。</span>
        )}
        {!newSession && state != null && !state.pending && !readOnly && !state.error
          && effectiveThresholdTokens == null && (
            <span>设置已应用；上下文用量将在下一次可靠读取后更新。</span>
          )}
      </div>
    </div>
  );
}

export default AutoCompactControl;
