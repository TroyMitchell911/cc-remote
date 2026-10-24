import type { ToolBlock } from "../domain/conversation";
import { ToolGroup } from "./ToolGroup";

/** Native PTC enclosure, never guessed from tool names or completion order. */
export default function DshToolTree({ tools, active }: { tools: ToolBlock[]; active: boolean }) {
  const ids = new Set(tools.map(tool => tool.tool_use_id));
  const roots = tools.filter(tool => !tool.parent_id || !ids.has(tool.parent_id));
  const rendered = new Set<string>();
  const byParent = new Map<string, ToolBlock[]>();
  for (const tool of tools) {
    if (tool.parent_id) byParent.set(tool.parent_id, [...(byParent.get(tool.parent_id) ?? []), tool]);
  }
  const render = (tool: ToolBlock, visited: Set<string>) => {
    rendered.add(tool.tool_use_id);
    const path = new Set([...visited, tool.tool_use_id]);
    const children = (byParent.get(tool.tool_use_id) ?? []).filter(child => !path.has(child.tool_use_id));
    return <div key={tool.tool_use_id} className="dsh-tool-node">
      <ToolGroup tools={[tool]} active={active} />
      {children.length > 0 && path.size < 32 && <details className="dsh-tool-children">
        <summary>{children.length} 个内部调用</summary>
        {children.map(child => render(child, path))}
      </details>}
    </div>;
  };
  if (!tools.some(tool => tool.parent_id && ids.has(tool.parent_id))) return <ToolGroup tools={tools} active={active} />;
  const tree = roots.map(tool => render(tool, new Set()));
  // Truncated/cyclic relationships must not hide otherwise readable tools.
  const remaining = tools.filter(tool => !rendered.has(tool.tool_use_id));
  return <div className="dsh-tool-tree">{tree}{remaining.length > 0 && <ToolGroup tools={remaining} active={active} />}</div>;
}
