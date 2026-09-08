const DIRECTIVE = ":codex-file-citation{";
const MAX_PATH_BYTES = 4096;
const MAX_BODY_CHARS = 8192;
const MAX_METADATA_CHARS = 512;
const WINDOWS_ABSOLUTE_PATH = /^[A-Za-z]:[\\/]/;
const ATTRIBUTE = /\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*("(?:\\.|[^"\\])*")/gy;
const UTF8 = new TextEncoder();

export interface CodexFileCitation {
  path: string;
  purpose: "output" | "source";
  artifactKind?: string;
  label?: string;
}

interface ParsedDirective {
  citation: CodexFileCitation;
  end: number;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
}

interface ScannedDirective {
  parsed: ParsedDirective | null;
  next: number;
  stop: boolean;
}

function safeText(value: string, maxChars?: number): boolean {
  if (maxChars !== undefined && value.length > maxChars) return false;
  for (const char of value) {
    const code = char.codePointAt(0)!;
    if (code < 32 || code === 127 || (code >= 0xD800 && code <= 0xDFFF)) {
      return false;
    }
  }
  return true;
}

function validCitation(attributes: Map<string, string>): CodexFileCitation | null {
  const path = attributes.get("path");
  const purpose = attributes.get("purpose");
  const artifactKind = attributes.get("artifact_kind");
  const label = attributes.get("label");
  if (!path
      || (!(path.startsWith("/") && !path.startsWith("//"))
        && !WINDOWS_ABSOLUTE_PATH.test(path))
      || !safeText(path)
      || UTF8.encode(path).length > MAX_PATH_BYTES
      || (purpose !== undefined && purpose !== "output" && purpose !== "source")
      || (artifactKind !== undefined
        && !safeText(artifactKind, MAX_METADATA_CHARS))
      || (label !== undefined && !safeText(label, MAX_METADATA_CHARS))) {
    return null;
  }
  return {
    path,
    purpose: purpose === "output" ? "output" : "source",
    ...(artifactKind ? { artifactKind } : {}),
    ...(label ? { label } : {}),
  };
}

function parseAttributes(body: string): CodexFileCitation | null {
  const attributes = new Map<string, string>();
  let cursor = 0;
  while (cursor < body.length) {
    ATTRIBUTE.lastIndex = cursor;
    const match = ATTRIBUTE.exec(body);
    if (!match) {
      return /^\s*$/.test(body.slice(cursor))
        ? validCitation(attributes)
        : null;
    }
    if (attributes.has(match[1])) return null;
    let value: unknown;
    try {
      value = JSON.parse(match[2]);
    } catch {
      return null;
    }
    if (typeof value !== "string") return null;
    if (match[1] !== "path" && !safeText(value, MAX_METADATA_CHARS)) return null;
    attributes.set(match[1], value);
    cursor = ATTRIBUTE.lastIndex;
  }
  return validCitation(attributes);
}

/** Scan one candidate without revisiting its suffix. A nested directive outside
 * a quoted value supersedes a malformed candidate so the full text walk stays
 * linear even while a response contains many incomplete prefixes. */
function scanDirective(text: string, start: number): ScannedDirective {
  const bodyStart = start + DIRECTIVE.length;
  let quoted = false;
  let escaped = false;
  for (let cursor = bodyStart; cursor < text.length; cursor += 1) {
    if (cursor - bodyStart > MAX_BODY_CHARS) {
      return { parsed: null, next: cursor, stop: false };
    }
    const char = text[cursor];
    if (quoted) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') quoted = false;
      continue;
    }
    if (char === '"') {
      quoted = true;
      continue;
    }
    if (char === "}") {
      const citation = parseAttributes(text.slice(bodyStart, cursor));
      return {
        parsed: citation ? { citation, end: cursor + 1 } : null,
        next: cursor + 1,
        stop: false,
      };
    }
    if (text.startsWith(DIRECTIVE, cursor)) {
      return { parsed: null, next: cursor, stop: false };
    }
  }
  return { parsed: null, next: text.length, stop: true };
}

export function parseCodexFileCitationDirective(
  text: string,
  start = 0,
): ParsedDirective | null {
  if (!text.startsWith(DIRECTIVE, start)) return null;
  return scanDirective(text, start).parsed;
}

function citationNodes(text: string): MarkdownNode[] | null {
  const nodes: MarkdownNode[] = [];
  let plainStart = 0;
  let search = 0;
  while (search < text.length) {
    const start = text.indexOf(DIRECTIVE, search);
    if (start < 0) break;
    const scanned = scanDirective(text, start);
    const parsed = scanned.parsed;
    if (parsed) {
      if (start > plainStart) {
        nodes.push({ type: "text", value: text.slice(plainStart, start) });
      }
      nodes.push({
        type: "link",
        url: encodeURIComponent(parsed.citation.path),
        title: [
          "cc-remote-file-citation",
          parsed.citation.purpose,
          parsed.citation.artifactKind ?? "",
        ].join(":"),
        children: [{
          type: "text",
          value: parsed.citation.path.split(/[\\/]/).pop() || "文件",
        }],
      });
      plainStart = parsed.end;
    }
    search = scanned.next;
    if (scanned.stop) break;
  }
  if (plainStart === 0) return null;
  if (plainStart < text.length) {
    nodes.push({ type: "text", value: text.slice(plainStart) });
  }
  return nodes;
}

function transformCitations(node: MarkdownNode): void {
  if (!node.children || node.type === "link" || node.type === "linkReference") return;
  for (let index = 0; index < node.children.length;) {
    const child = node.children[index];
    const replacements = child.type === "text" && typeof child.value === "string"
      ? citationNodes(child.value) : null;
    if (replacements) {
      node.children.splice(index, 1, ...replacements);
      index += replacements.length;
    } else {
      transformCitations(child);
      index += 1;
    }
  }
}

/** Convert official file citations only in prose text nodes, not code/links. */
export function remarkCodexFileCitations(): (tree: MarkdownNode) => void {
  return transformCitations;
}
