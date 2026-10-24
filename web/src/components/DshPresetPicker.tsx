import type { DshPreset } from "../protocol";
import { Icon } from "../icons";
import { ChoicePicker } from "./ChoicePicker";
import "./DshPresetPicker.css";

export default function DshPresetPicker({ presets, value, onChange, disabled }: {
  presets: DshPreset[]; value?: string | null;
  onChange?: (value: string) => void; disabled: boolean;
}) {
  return <ChoicePicker className="newchat-profile dsh-preset-trigger" sheetClassName="dsh-preset-sheet"
    label="选择 DSH 模式" showChevron={false} value={value ?? ""}
    onChange={value => onChange?.(value)} disabled={disabled || !presets.length}
    options={presets.map(p => ({ value: p.id, label: p.name, description: p.description, disabled: !p.available }))}>
    <Icon name="sliders" size={16} /><b>{presets.find(p => p.id === value)?.name ?? "选择模式"}</b>
  </ChoicePicker>;
}
