const ADDRESS_MAX_CHARS = 8_192;
const EXPLICIT_SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i;
const SEARCH_OPERATOR_RE = /^(?:site|cache|filetype|ext|related|intitle|allintitle|inurl|allinurl|before|after|source|define):\S+$/i;

export type BrowserAddressResolution =
  | { ok: true; url: string; kind: "url" | "search" }
  | { ok: false; message: string };

function looksLikeHost(value: string): boolean {
  if (/\s/.test(value)) return false;
  const candidate = value.startsWith("//") ? `https:${value}`
    : `https://${value}`;
  try {
    const parsed = new URL(candidate);
    if (parsed.username || parsed.password || !parsed.hostname) return false;
    const host = parsed.hostname.toLowerCase();
    return host === "localhost"
      || host.endsWith(".localhost")
      || host.includes(".")
      || /^\[[0-9a-f:.]+\]$/i.test(host)
      || /:\d{1,5}$/.test(value.split(/[/?#]/, 1)[0] ?? "");
  } catch {
    return false;
  }
}

/** Resolve the shared browser's omnibox without weakening wrapper URL policy. */
export function resolveBrowserAddress(raw: string): BrowserAddressResolution {
  const value = raw.trim();
  if (!value) return { ok: false, message: "请输入网址或搜索内容" };
  if (value.length > ADDRESS_MAX_CHARS) {
    return { ok: false, message: "网址或搜索内容过长" };
  }

  let target: string;
  if (/^https?:\/\//i.test(value)) {
    target = value;
  } else if (value.startsWith("//")) {
    target = `https:${value}`;
  } else if (looksLikeHost(value)) {
    target = `https://${value}`;
  } else if (
    EXPLICIT_SCHEME_RE.test(value)
    && !/\s/.test(value)
    && !SEARCH_OPERATOR_RE.test(value)
  ) {
    return { ok: false, message: "只支持 http:// 和 https:// 网页" };
  } else {
    target = `https://www.google.com/search?q=${encodeURIComponent(value)}`;
    if (target.length > ADDRESS_MAX_CHARS) {
      return { ok: false, message: "搜索内容过长" };
    }
    return { ok: true, url: target, kind: "search" };
  }

  try {
    const parsed = new URL(target);
    if (
      !["http:", "https:"].includes(parsed.protocol)
      || !parsed.hostname
      || parsed.username
      || parsed.password
    ) {
      return { ok: false, message: "请输入不含账号密码的 http(s) 网页地址" };
    }
    const url = parsed.toString();
    if (url.length > ADDRESS_MAX_CHARS) {
      return { ok: false, message: "网页地址过长" };
    }
    return { ok: true, url, kind: "url" };
  } catch {
    return { ok: false, message: "网页地址格式无效" };
  }
}
