import type { SessionLayout } from "../appearance";

const CHOICES = [
  { id: "comfortable", label: "舒展", description: "保留卡片与路径" },
  { id: "compact", label: "紧凑", description: "一屏显示更多会话" },
] as const;
const EXAMPLES = [
  { title: "cc-remote", path: "cc-remote", account: "nyx", tone: 1 },
  { title: "Robot-Agent", path: "workspace", account: "default", tone: 0 },
  { title: "Auriga", path: "auriga", account: "iris", tone: 2 },
  { title: "语音测试", path: "speech", account: "nyx", tone: 1 },
];

export function AppearanceSessionLayout({ value, onChange }: {
  value: SessionLayout;
  onChange: (value: SessionLayout) => void;
}) {
  return <section className="appearance-layout-section" aria-label="会话栏排布">
    <h3 className="appearance-label">会话栏排布</h3>
    <div className="appearance-layout-choices">
      {CHOICES.map(({ id, label, description }) => <button type="button" key={id}
        className="appearance-layout-option" aria-label={label} aria-pressed={value === id}
        onClick={() => onChange(id)}>
        <span className={`appearance-layout-preview ${id}`} aria-hidden="true">
          {EXAMPLES.slice(0, id === "comfortable" ? 2 : 4).map((example, index) =>
            <span className={`appearance-layout-row${index === 0 ? " selected" : ""}`} key={example.title}>
              <i className={`appearance-layout-account tone-${example.tone}`}>
                {id === "comfortable" ? example.account : null}
              </i>
              <b>{example.title}</b>
              {id === "comfortable" && <small>{example.path}</small>}
            </span>)}
        </span>
        <span className="appearance-layout-name">{label}</span>
        <span className="appearance-layout-description">{description}</span>
      </button>)}
    </div>
  </section>;
}
