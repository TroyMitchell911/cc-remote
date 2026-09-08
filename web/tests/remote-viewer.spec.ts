import { test, expect } from "@playwright/test";
import { PROTOCOL_VERSION } from "../src/protocol";

const ORIGIN = process.env.VIEWER_TEST_ORIGIN ?? "http://127.0.0.1:4174";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => { Object.defineProperty(window, "__fixtureNativeFetch", { value: window.fetch }); });
  // Test-only DNS mapping. Preserve real origins, browser cookie policy and
  // CSP; only the socket destination is redirected to the loopback fixtures.
  await page.route(/^http:\/\/(?:[a-z0-9-]+\.)*cc-remote\.localhost:\d+\//, async (route) => {
    const url = new URL(route.request().url());
    const backend = url.hostname !== "app.cc-remote.localhost" || url.pathname.startsWith("/api/");
    const headers = { ...await route.request().allHeaders(), host: url.host };
    const response = await route.fetch({
      url: `http://127.0.0.1:${backend ? 4178 : new URL(ORIGIN).port}${url.pathname}${url.search}`,
      headers, maxRedirects: 0,
    });
    await route.fulfill({ response });
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html`);
  const status = await page.evaluate(async () => (await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: "local-viewer-fixture-password" }),
  })).status);
  expect(status).toBe(200);
});

test("remote Viewer loads modules and binary models without exposing main cookies", async ({ page }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(frame.locator("#badge")).toHaveJSProperty("naturalWidth", await page.evaluate(() => devicePixelRatio > 1 ? 8 : 16));
  expect(await frame.locator("body").evaluate((node) => getComputedStyle(node).getPropertyValue("--fixture-imported").trim())).toBe("yes");
  if (!ORIGIN.includes("localhost") && !ORIGIN.includes("127.0.0.1")) expect(await page.evaluate(() => isSecureContext)).toBe(false);
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const state = await frame.locator("body").evaluate(() => (window as unknown as {
    viewerState: { bytes: number; parentBlocked: boolean; rotations: number; webgl: boolean };
  }).viewerState);
  expect(state.bytes).toBe(18 * 8192);
  expect(state.parentBlocked).toBe(true);
  expect(state.rotations).toBe(1);
  expect(state.webgl).toBe(true);
  const source = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", source!);
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const canFetchMain = await frame.locator("body").evaluate(async (_, origin) => {
    try { await fetch(`${origin}/api/session`, { credentials: "include" }); return true; } catch { return false; }
  }, ORIGIN);
  expect(canFetchMain).toBe(false);
  await page.screenshot({ path: test.info().outputPath("viewer.png"), fullPage: true });
});

test("Bridge blocks storage and native network access, and logout clears the frame", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Opaque-frame behavior is specific to Bridge.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  const protection = await frame.locator("body").evaluate(async (_, origin) => {
    let cookieBlocked = false, storageBlocked = false, networkBlocked = false;
    try { void document.cookie; } catch { cookieBlocked = true; }
    try { void localStorage.length; } catch { storageBlocked = true; }
    try {
      const original = (window as unknown as { __fixtureNativeFetch: typeof fetch }).__fixtureNativeFetch;
      await original(`${origin}/api/session`, { credentials: "include" });
    } catch { networkBlocked = true; }
    return { cookieBlocked, storageBlocked, networkBlocked };
  }, ORIGIN);
  expect(protection).toEqual({ cookieBlocked: true, storageBlocked: true, networkBlocked: true });
  // The unsupported native request produces a useful warning, not a network
  // permission prompt. Explicitly reconnect to exercise live logout revocation.
  await expect(page.locator(".viewer-error-banner")).toBeVisible();
  await page.getByRole("button", { name: "重新连接", exact: true }).click();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  await page.evaluate(() => fetch("/api/logout", { method: "POST" }));
  await expect(page.locator(".viewer-stage iframe")).toHaveCount(0, { timeout: 5000 });
});

test("Bridge loads multi-MiB embedded data and rejects oversized HTML with a precise error", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge text budgets.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^内嵌模型测试/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#embedded-status")).toHaveText("内嵌模型已加载 · 3145728", { timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  await frame.getByRole("button", { name: "旋转", exact: true }).click();
  const src = await page.locator("iframe").getAttribute("src");
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.locator("iframe")).toHaveAttribute("src", src!);
  await expect(frame.locator("#embedded-status")).toHaveText("已旋转");
  await page.getByRole("button", { name: "切换预览", exact: true }).click();
  await page.getByRole("button", { name: /^HTML 超限测试/ }).click();
  const error = page.locator(".viewer-error-banner");
  await expect(error).toContainText("预览 HTML过大", { timeout: 20000 });
  await expect(error).toContainText("/viewer/too-large.html");
  await expect(error).toContainText("16777217 字节");
  await expect(error).toContainText("上限 16 MiB");
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
});

test("Bridge provides Range, HEAD, cancellation and bounded parallel fetches", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge adapter coverage.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  const result = await frame.locator("body").evaluate(async () => {
    const path = "../meshes/model.stl";
    const head = await fetch(path, { method: "HEAD" });
    const range = await fetch(path, { headers: { Range: "bytes=2-7" } });
    const cached = await fetch(path, { headers: { "If-None-Match": head.headers.get("etag")! } });
    const cancelled = await fetch(path);
    await cancelled.body!.cancel();
    const aborter = new AbortController(); aborter.abort();
    let aborted = false;
    try { await fetch(path, { signal: aborter.signal }); } catch { aborted = true; }
    const burst = await Promise.all(Array.from({ length: 43 }, async (_, index) => {
      const response = await fetch(`${path}?v=${index}`);
      return (await response.arrayBuffer()).byteLength;
    }));
    return { head: head.status, headBytes: (await head.arrayBuffer()).byteLength,
      range: range.status, rangeBytes: (await range.arrayBuffer()).byteLength,
      cached: cached.status, aborted, burst };
  });
  expect(result).toEqual({ head: 200, headBytes: 0, range: 206, rangeBytes: 6,
    cached: 304, aborted: true, burst: Array(43).fill(18 * 8192) });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});

test("Bridge explains unsupported modules and failed CDN dependencies without weakening isolation", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge adapter coverage.");
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^动态模块限制测试/ }).click();
  await expect(page.locator(".viewer-error-banner")).toContainText("运行时动态模块地址", { timeout: 20000 });
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  await page.getByRole("button", { name: "切换预览", exact: true }).click();
  await page.route("https://esm.sh/cc-remote-fixture-dependency", (route) => route.abort());
  await page.getByRole("button", { name: /^依赖断网测试/ }).click();
  await expect(page.locator(".viewer-error-banner")).toContainText(/脚本|模块|加载|网络/, { timeout: 20000 });
  await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
});

for (const api of ["WebSocket", "XMLHttpRequest", "EventSource", "Worker", "SharedWorker"]) {
  test(`Bridge rejects ${api} without suggesting a mode switch will connect a backend`, async ({ page }) => {
    test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge compiler diagnostics.");
    await page.getByRole("button", { name: "打开远程预览" }).click();
    await page.getByRole("button", { name: new RegExp(`^${api} 限制测试`) }).click();
    const error = page.locator(".viewer-error-banner");
    await expect(error).toContainText(`此页面使用 ${api}，Bridge 暂不支持。`, { timeout: 20000 });
    if (["Worker", "SharedWorker"].includes(api)) {
      await expect(error).toContainText("请使用不依赖 Worker 的静态页面。");
      await expect(error).not.toContainText("后台连接");
    } else {
      await expect(error).toContainText("当前预览不代理后台连接，切换 Isolated 模式也无法接通后台服务。");
    }
    await expect(error).not.toContainText("请使用 Isolated 模式");
    await expect(page.locator(".viewer-stage iframe")).toHaveAttribute("sandbox", "allow-scripts");
  });
}

test("an unrelated opaque frame cannot steal the Bridge initialization port", async ({ page }) => {
  test.skip(process.env.VIEWER_TEST_MODE === "isolated", "Bridge handshake coverage.");
  let releaseRunner!: () => void;
  const delayed = new Promise<void>((resolve) => { releaseRunner = resolve; });
  await page.route("**/__cc_viewer/bridge/*", async (route) => { await delayed; await route.continue(); });
  await page.getByRole("button", { name: "打开远程预览" }).click();
  const opened = page.waitForResponse((response) => response.url().endsWith("/api/viewers/open"));
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  const grant = await (await opened).json();
  try {
    const stolen = await page.evaluate(async ({ id, version }: { id: string; version: number }) => {
      const attack = document.createElement("iframe");
      attack.sandbox.add("allow-scripts");
      attack.hidden = true;
      const result = new Promise<boolean>((resolve) => {
        let received = false;
        const listener = (event: MessageEvent) => {
          if (event.source === attack.contentWindow && event.data === "port-stolen") received = true;
          if (event.source === attack.contentWindow && event.data === "probe-done") {
            window.removeEventListener("message", listener); resolve(received);
          }
        };
        window.addEventListener("message", listener);
      });
      attack.srcdoc = `<script>
        addEventListener('message',e=>{if(e.ports.length)parent.postMessage('port-stolen','*')});
        parent.postMessage({type:'cc-viewer-bridge-ready',id:${JSON.stringify(id)},v:${version}},'*');
        setTimeout(()=>parent.postMessage('probe-done','*'),250);
        </script>`;
      document.body.append(attack);
      const value = await result;
      attack.remove();
      return value;
    }, { id: grant.id, version: PROTOCOL_VERSION });
    expect(stolen).toBe(false);
  } finally { releaseRunner(); }
  await expect(page.frameLocator(".viewer-stage iframe").locator("#status"))
    .toContainText("模型已加载", { timeout: 20000 });
});

test("remote Viewer survives refresh, keeps session scope and closes without deleting files", async ({ page, isMobile }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  const oldSource = await page.locator("iframe").getAttribute("src");
  await page.reload();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  expect(await page.locator("iframe").getAttribute("src")).not.toBe(oldSource);
  const stored = await page.evaluate(() => sessionStorage.getItem("cc-remote:viewer-panels-v1"));
  expect(stored).not.toContain("origin");
  expect(stored).not.toContain("cookie");
  if (!isMobile) {
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByRole("complementary", { name: "远程预览" })).toHaveCount(0);
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  }
  await page.getByRole("button", { name: isMobile ? "返回对话" : "关闭预览", exact: true }).click();
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
});

test("remote Viewer uses one desktop side panel and fills the mobile viewport", async ({ page, isMobile }) => {
  await page.getByRole("button", { name: "打开远程预览" }).click();
  const panel = page.locator(".remote-viewer-panel");
  await expect(panel).toBeVisible();
  const box = await panel.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box).not.toBeNull();
  if (isMobile) {
    expect(box!.x).toBe(0);
    expect(box!.width).toBe(viewport.width);
    expect(box!.height).toBeCloseTo(viewport.height, 2);
    await expect(page.getByRole("button", { name: "返回对话", exact: true })).toBeVisible();
  } else {
    expect(box!.width).toBeLessThan(viewport.width * 0.6);
    await page.getByRole("button", { name: "放大预览" }).click();
    expect((await panel.boundingBox())!.width).toBeGreaterThan(viewport.width * 0.9);
    await page.getByRole("button", { name: "还原大小" }).click();
    expect((await panel.boundingBox())!.width).toBeLessThan(viewport.width * 0.6);
  }
});

test("remote Viewer reacquires authorization after a cached page is restored", async ({ page }) => {
  const bindings: string[] = [];
  page.on("websocket", (socket) => {
    if (!socket.url().endsWith("/ws/viewer-client")) return;
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      const message = JSON.parse(payload);
      if (message.type === "bind") bindings.push(message.id);
    });
  });
  await page.getByRole("button", { name: "打开远程预览" }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  const source = await page.locator("iframe").getAttribute("src");
  const oldBindings = [...bindings];
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/open", async (route) => {
    await held;
    await route.fallback();
  });
  const opening = page.waitForRequest((request) => request.url().endsWith("/api/viewers/open"));
  const opened = page.waitForResponse((response) => response.url().endsWith("/api/viewers/open"));
  try {
    await page.evaluate(() => {
      window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    });
    await opening;
    // The catalog is back, but the replacement lease is not. Do not remount
    // the revoked frame in this gap or let its 403 retire the new lease.
    await expect(page.locator("iframe")).toHaveCount(0);
    expect(bindings).toEqual(oldBindings);
  } finally { release(); }
  const replacement = await (await opened).json();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(page.locator("iframe")).not.toHaveAttribute("src", source!);
  await expect(page.getByRole("alert")).toHaveCount(0);
  if (replacement.mode === "bridge") expect(bindings).toEqual([...oldBindings, replacement.id]);
  await page.frameLocator("iframe").getByRole("button", { name: "旋转模型" }).click();
  await expect(page.frameLocator("iframe").locator("#status")).toHaveText("已旋转 1 次");
});

test("session page discovery is explicit, durable and leaves cloud links alone", async ({ page, isMobile }) => {
  const sid = `artifact-${Date.now()}`;
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=${sid}`);
  await page.getByRole("button", { name: "显示页面产物" }).click();
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  await expect(preview).toBeVisible();
  await expect(preview).toHaveCount(1);
  await expect(page.getByRole("link", { name: "GitHub", exact: true }))
    .toHaveAttribute("href", "https://github.com/demo/index.html");
  await expect(page.locator(".remote-viewer-panel")).toHaveCount(0);
  await preview.click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await page.locator("#append-message").evaluate((button: HTMLButtonElement) => button.click());
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  await expect(page.locator(".remote-viewer-panel")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: test.info().outputPath("session-page-preview.png"), fullPage: true });
  await page.getByRole("button", { name: isMobile ? "返回对话" : "关闭预览", exact: true }).click();
  // Browser panel memory is not the source of session associations.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await page.getByRole("button", { name: "打开远程预览", exact: true }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^动态模块限制测试/ })).toHaveCount(0);
  await page.getByRole("button", { name: "移除关联 机器人结构", exact: true }).click();
  await expect(page.getByText("本会话还没有页面", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
  await page.getByRole("button", { name: /^机器人结构/ }).click();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 20000 });
});

test("opening the picker in another session does not copy the global catalog", async ({ page }) => {
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=empty-${Date.now()}`);
  await page.getByRole("button", { name: "打开远程预览", exact: true }).click();
  await expect(page.getByText("本会话还没有页面", { exact: true })).toBeVisible();
  await expect(page.locator(".viewer-site")).toHaveCount(0);
  await page.getByRole("button", { name: "+ 关联已有预览", exact: true }).click();
  await expect(page.getByRole("button", { name: /^机器人结构/ })).toBeVisible();
  await page.getByRole("button", { name: "返回本会话", exact: true }).click();
  await expect(page.locator(".viewer-site")).toHaveCount(0);
});

test("page entry retries while the answer is streaming without a session switch", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/viewers/pages", async (route) => {
    if (route.request().postDataJSON()?.action === "resolve" && ++attempts === 1) {
      await route.fulfill({ status: 503, json: { error: "device_offline" } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=stream-${Date.now()}`);
  await page.getByRole("button", { name: "开始流式页面产物", exact: true }).click();
  await expect(page.locator("p").filter({ hasText: "页面已做好：本地页面" })).toBeVisible();
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  await expect(preview).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole("button", { name: "完成回复", exact: true })).toBeVisible();
  expect(attempts).toBe(2);
  await expect(preview).toHaveCSS("border-top-width", "0px");
  await expect(preview).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(page.locator(".page-preview-link > span")).toHaveCount(0);
  await preview.click();
  await expect(page.frameLocator("iframe").locator("#status")).toContainText("模型已加载", { timeout: 20000 });
});

test("finishing an answer while page discovery is in flight still brings up its entry", async ({ page }) => {
  let attempts = 0;
  let started!: () => void;
  const firstRequest = new Promise<void>((resolve) => { started = resolve; });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/pages", async (route) => {
    if (route.request().postDataJSON()?.action === "resolve" && ++attempts === 1) {
      started();
      await delayed;
      await route.fulfill({ json: { pages: [] } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=boundary-${Date.now()}`);
  try {
    await page.getByRole("button", { name: "开始流式页面产物", exact: true }).click();
    await firstRequest;
    await expect(page.getByRole("button", { name: "查看页面", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "完成回复", exact: true }).click();
  } finally { release(); }
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 10000 });
  expect(attempts).toBe(2);
  await expect(page.getByRole("button", { name: "完成回复", exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("lightweight-page-entry.png"), fullPage: true });
});

test("a discovery superseded by a list refresh is retried instead of remembered as invisible", async ({ page }) => {
  let attempts = 0;
  let started!: () => void;
  const firstRequest = new Promise<void>((resolve) => { started = resolve; });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/viewers/pages", async (route) => {
    const action = route.request().postDataJSON()?.action;
    if (action === "resolve" && ++attempts === 1) {
      const response = await route.fetch();
      started();
      await delayed;
      await route.fulfill({ response });
    } else if (action === "list" && attempts === 1) {
      await route.fulfill({ json: { pages: [] } });
    } else await route.continue();
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=refresh-race-${Date.now()}`);
  try {
    await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
    await firstRequest;
    const refreshed = page.waitForResponse((response) => response.url().endsWith("/api/viewers/pages")
      && response.request().postDataJSON()?.action === "list");
    await page.getByRole("button", { name: "刷新页面关联", exact: true }).click();
    await refreshed;
  } finally { release(); }
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 10000 });
  expect(attempts).toBe(2);
});

test("page metadata never shifts the completion footer on cold load or A-B-A focus", async ({ page }) => {
  let release!: () => void;
  let delayed = new Promise<void>((resolve) => { release = resolve; });
  let hold = true;
  const pages = [{ id: "layout-page", machine_id: "render-device", site_id: "robot", entry: "/viewer/index.html",
    label: "页面", references: ["viewer/index.html"], turn_ids: ["page-turn"], available: true }];
  await page.route("**/api/viewers/pages", async (route) => {
    if (hold) await delayed;
    const scope = route.request().postDataJSON().scope;
    await route.fulfill({ json: { pages: scope.sid === "session-a" ? pages : [] } });
  });
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=session-a&chat-layout=1`);
  const mark = page.locator(".turn-done-mark");
  const preview = page.getByRole("button", { name: "查看页面", exact: true });
  try {
    await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
    await expect(mark).toBeVisible();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const initial = await mark.boundingBox();
    expect(initial).not.toBeNull();
    await expect(preview).toHaveCount(0);
    hold = false; release();
    await expect(preview).toBeVisible();
    expect(Math.abs((await mark.boundingBox())!.y - initial!.y)).toBeLessThan(1);
    expect(await page.locator(".ai-meta").evaluate((el) => el.getBoundingClientRect().height)).toBe(22);

    hold = true;
    delayed = new Promise<void>((resolve) => { release = resolve; });
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByTestId("current-sid")).toHaveText("session-b");
    await expect(preview).toHaveCount(0);
    // The chat/scroll root is retained; individual session rows may re-key.
    await page.locator(".thread-shell").evaluate((el) => el.setAttribute("data-retained", "yes"));
    await page.getByRole("button", { name: "切换会话", exact: true }).click();
    await expect(page.getByTestId("current-sid")).toHaveText("session-a");
    await expect(preview).toBeVisible(); // list/resolve are still deliberately blocked
    await expect(page.locator(".thread-shell")).toHaveAttribute("data-retained", "yes");
    const restored = (await mark.boundingBox())!.y;
    hold = false; release();
    await expect(preview).toBeVisible();
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    expect(Math.abs((await mark.boundingBox())!.y - restored)).toBeLessThan(1);
    await page.screenshot({ path: test.info().outputPath("stable-page-footer.png"), fullPage: true });
  } finally { hold = false; release(); }
});

test("multiple page entries use one footer slot and an out-of-flow menu", async ({ page }) => {
  await page.route("**/api/viewers/pages", async (route) => route.fulfill({ json: { pages: [1, 2, 3].map((id) => ({
    id: `layout-${id}`, machine_id: "render-device", site_id: "robot", entry: `/viewer/${id}.html`,
    label: `页面 ${id}`, references: [], turn_ids: ["page-turn"], available: true,
  })) } }));
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=session-a&chat-layout=1`);
  await page.getByRole("button", { name: "显示页面产物", exact: true }).click();
  const mark = page.locator(".turn-done-mark");
  const trigger = page.locator(".page-preview-menu > summary");
  await expect(trigger).toBeVisible();
  const before = (await mark.boundingBox())!.y;
  await trigger.click();
  await expect(page.locator(".page-preview-options button")).toHaveCount(3);
  expect(Math.abs((await mark.boundingBox())!.y - before)).toBeLessThan(1);
});

test("home static-server link becomes a session preview without registration or global clutter", async ({ page }) => {
  await page.goto(`${ORIGIN}/tests/remote-viewer.html?sid=home-${Date.now()}`);
  await page.getByRole("button", { name: "显示未登记页面", exact: true }).click();
  await expect(page.getByRole("button", { name: "查看页面", exact: true })).toBeVisible({ timeout: 20000 });
  // A confirmed local link itself is interactive too; cloud links stay native.
  await page.getByRole("link", { name: "本地页面", exact: true }).click();
  const frame = page.frameLocator(".viewer-stage iframe");
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await frame.getByRole("button", { name: "旋转模型" }).click();
  await expect(frame.locator("#status")).toHaveText("已旋转 1 次");
  const catalog = await page.evaluate(async () => (await (await fetch("/api/viewers")).json()).sites);
  expect(catalog.some((site: { id: string }) => site.id.startsWith("auto-"))).toBe(false);
  await page.reload();
  await expect(frame.locator("#status")).toContainText("模型已加载", { timeout: 30000 });
  await expect(page.locator(".viewer-error-banner")).toHaveCount(0);
});
