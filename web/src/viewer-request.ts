const ERRORS: Record<string, string> = {
  session_expired: "登录已过期，请重新登录。",
  viewer_not_configured: "远程预览已关闭，请在 Relay 配置中启用。",
  device_offline: "设备已离线，连接恢复后可重新打开。",
  publication_missing: "该预览已移除，请选择其他预览。",
  publication_changed: "预览配置已更新，请重新打开。",
  preview_expired: "预览已过期，请重新打开。",
  preview_missing: "预览已失效，请重新打开。",
  preview_capacity: "打开的预览过多，请先关闭不使用的预览。",
  viewer_busy: "当前资源加载较多，请稍后重试。",
  origin_rejected: "预览来源验证失败，请从配置的主站地址打开。",
  page_request_failed: "暂时无法读取本会话的页面，请刷新重试。",
};

export async function viewerRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, credentials: "same-origin", cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ViewerRequestError(response.status, data.error);
  return data as T;
}

export class ViewerRequestError extends Error {
  status: number;
  constructor(status: number, code: string) {
    super(ERRORS[code] ?? "暂时无法连接远程预览，请稍后重试。");
    this.status = status;
  }
}
