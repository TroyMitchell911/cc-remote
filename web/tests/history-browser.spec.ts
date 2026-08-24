import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

test("mounted message image retries once when cache capacity is released", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-capacity=1");

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("1");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("0");
  await expect(page.locator(".message-image-error"))
    .toContainText("图片暂时无法加载");

  await page.getByTestId("release-inline-capacity").click();

  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
  await expect(page.locator(".message-image-loading")).toBeVisible();
  await page.waitForTimeout(150);
  await expect(page.getByTestId("inline-load-attempts")).toHaveText("2");
  await expect(page.getByTestId("inline-network-loads")).toHaveText("1");
});

test("two visible images do not reclaim one cache slot forever", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?inline-image-eviction=1");

  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");
  const visibleImages = page.getByTestId("two-visible-inline-images");
  await expect(visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  })).toBeVisible();
  await expect(visibleImages.getByRole("img", { name: "B" })).toBeAttached();

  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("3");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("2");

  await visibleImages.getByRole("button", {
    name: "图片暂时无法加载，点击重试",
  }).click();
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");
  await expect(visibleImages.getByRole("img", { name: "A" })).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("visible-inline-attempts")).toHaveText("4");
  await expect(page.getByTestId("visible-inline-network-loads")).toHaveText("3");

  const noLoader = page.getByTestId("inline-no-loader");
  await expect(noLoader.getByText("图片加载失败", { exact: true })).toBeVisible();
  await expect(noLoader.getByText("图片加载超时", { exact: true })).toBeVisible();
  await expect(noLoader.locator("button.message-image-error")).toHaveCount(0);
});

const productionCsp = (() => {
  const template = readFileSync(
    new URL("../../deploy/Caddyfile", import.meta.url),
    "utf8",
  );
  const match = template.match(/Content-Security-Policy "([^"]+)"/);
  if (!match) throw new Error("production Content-Security-Policy is missing");
  return match[1].replace(
    "wss://cc-remote.example.com",
    "ws://127.0.0.1:4174",
  );
})();

async function applyProductionCsp(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.evaluate((policy) => {
    const meta = document.createElement("meta");
    meta.httpEquiv = "Content-Security-Policy";
    meta.content = policy;
    document.head.append(meta);
  }, productionCsp);
}

test("HTML preview retains head CSS and runs scripts only after explicit consent", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?artifact-html=1");
  await applyProductionCsp(page);

  const previewGeometry = async () => page.locator(
    ".artifact-html-stage",
  ).evaluate((stage) => {
    const body = stage.parentElement;
    const frame = stage.querySelector("iframe");
    return {
      bodyWidth: body?.getBoundingClientRect().width ?? 0,
      stageWidth: stage.getBoundingClientRect().width,
      frameWidth: frame?.getBoundingClientRect().width ?? 0,
    };
  });
  const expectPreviewFillsBody = async () => {
    await expect.poll(async () => {
      const geometry = await previewGeometry();
      return geometry.bodyWidth > 0
        && Math.abs(geometry.stageWidth - geometry.bodyWidth) <= 1
        && Math.abs(geometry.frameWidth - geometry.bodyWidth) <= 1;
    }).toBe(true);
  };

  const staticFrame = page.frameLocator('iframe[title="HTML 静态预览"]');
  await expectPreviewFillsBody();
  await expect(staticFrame.locator("#head-style")).toHaveCSS(
    "color",
    "rgb(12, 34, 56)",
  );
  await expect(staticFrame.locator("#visualization-theme")).toHaveCSS(
    "color",
    "rgb(236, 237, 243)",
  );
  await expect(staticFrame.locator("#visualization-theme")).toHaveCSS(
    "background-color",
    "rgb(13, 14, 21)",
  );
  await expect(staticFrame.locator("#visualization-theme")).toHaveCSS(
    "border-top-color",
    "rgb(49, 50, 63)",
  );
  await expect(staticFrame.locator("body")).not.toHaveAttribute(
    "data-script-ran",
    "yes",
  );

  await page.getByRole("button", { name: "运行交互预览" }).click();
  const interactiveFrame = page.frameLocator('iframe[title="HTML 交互预览"]');
  await expectPreviewFillsBody();
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
  await expect(interactiveFrame.locator("body")).toHaveAttribute(
    "data-parent-blocked",
    "yes",
  );
  await expect(page.locator("body")).not.toHaveAttribute(
    "data-preview-escaped",
    "yes",
  );

  await page.getByRole("button", { name: "停止交互预览" }).click();
  await page.getByRole("button", { name: "运行交互预览" }).click();
  const warmInteractiveFrame = page.frameLocator(
    'iframe[title="HTML 交互预览"]',
  );
  await expect(warmInteractiveFrame.locator("body")).toHaveAttribute(
    "data-script-ran",
    "yes",
  );
});

test("Codex visualize output opens its local HTML through the preview callback", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?codex-visualization=1");
  const card = page.getByRole("button", { name: /结构原理草图/ });
  await expect(card).toBeVisible();
  await expect(card).toContainText("HTML 可视化");
  await expect(page.locator("main")).not.toContainText("/tmp/private");
  await card.click();
  await expect(page.getByTestId("visualization-opened-path")).toHaveText(
    "/tmp/private/concept.html",
  );
});

for (const fixture of ["artifact-svg", "artifact-markdown-svg"] as const) {
  test(`${fixture} sanitizes SVG before creating a blob URL`, async ({
    page,
  }) => {
    await page.goto(`/tests/history-browser.html?${fixture}=1`);
    const image = page.getByRole("img", { name: fixture === "artifact-svg"
      ? "diagram.svg" : "diagram" });
    await expect(image).toBeVisible();
    const sanitized = await image.evaluate(async (node) => {
      const src = (node as HTMLImageElement).src;
      return {
        src,
        text: await (await fetch(src)).text(),
      };
    });
    expect(sanitized.src).toMatch(/^blob:/);
    expect(sanitized.text).toContain("safe-svg-rect");
    expect(sanitized.text).not.toMatch(
      /<script|foreignObject|example\.com|<image/i,
    );
    await applyProductionCsp(page);
    await expect(image).toBeVisible();
  });
}

test("mobile Markdown source editor fills the available artifact body", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?artifact-markdown-source=1");
  await page.getByRole("button", { name: "源码" }).click();
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toBeVisible();

  const measure = () => page.locator(".source-artifact-body").evaluate((body) => {
    const textarea = body.querySelector<HTMLTextAreaElement>(".markdown-editor");
    if (!textarea) throw new Error("Markdown editor is missing");
    const bodyRect = body.getBoundingClientRect();
    const editorRect = textarea.getBoundingClientRect();
    return {
      bodyHeight: bodyRect.height,
      editorHeight: editorRect.height,
      bottomGap: bodyRect.bottom - editorRect.bottom,
      scrollHeight: textarea.scrollHeight,
      clientHeight: textarea.clientHeight,
    };
  });

  let geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.8);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
  expect(geometry.scrollHeight).toBeGreaterThan(geometry.clientHeight);

  await page.setViewportSize({ width: 390, height: 480 });
  geometry = await measure();
  expect(geometry.editorHeight).toBeGreaterThan(geometry.bodyHeight * 0.75);
  expect(geometry.bottomGap).toBeLessThanOrEqual(16);
});

test("dark desktop code block and copy action stay visually distinct", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/tests/history-browser.html?code-copy-theme=1&theme=dark");
  await page.waitForFunction(() =>
    document.documentElement.dataset.theme === "dark"
  );
  await page.waitForTimeout(200);
  const copy = page.getByRole("button", { name: "复制代码" });
  await expect(copy).toBeVisible();

  const appearance = await copy.evaluate((button) => {
    const sample = (color: string) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas context unavailable");
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    };
    const composite = (front: number[], back: number[]) => {
      const alpha = front[3] / 255;
      return front.slice(0, 3).map((value, index) =>
        value * alpha + back[index] * (1 - alpha)
      );
    };
    const luminance = (color: number[]) => {
      const channels = color.slice(0, 3).map((value) => {
        const normalized = value / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152
        + channels[2] * 0.0722;
    };
    const block = button.closest(".message-code-block");
    const code = block?.querySelector("pre");
    const pageSurface = button.closest("main");
    if (!code || !pageSurface) throw new Error("code block is missing");
    const foreground = sample(getComputedStyle(button).color);
    const expectedForeground = sample(
      getComputedStyle(document.documentElement).getPropertyValue("--text"),
    );
    const buttonBackground = sample(getComputedStyle(button).backgroundColor);
    const codeBackground = sample(getComputedStyle(code).backgroundColor);
    const pageBackground = sample(getComputedStyle(pageSurface).backgroundColor);
    const effectiveCodeBackground = composite(codeBackground, pageBackground);
    const effectiveButtonBackground = composite(
      buttonBackground,
      effectiveCodeBackground,
    );
    const lighter = Math.max(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const darker = Math.min(
      luminance(foreground),
      luminance(effectiveButtonBackground),
    );
    const codeLighter = Math.max(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeDarker = Math.min(
      luminance(effectiveCodeBackground),
      luminance(pageBackground),
    );
    const codeBackgroundDelta = effectiveCodeBackground.reduce(
      (total, value, index) => total + Math.abs(value - pageBackground[index]),
      0,
    ) / 3;
    return {
      contrast: (lighter + 0.05) / (darker + 0.05),
      codeBackgroundContrast: (codeLighter + 0.05) / (codeDarker + 0.05),
      codeBackgroundDelta,
      foreground,
      expectedForeground,
    };
  });

  expect(appearance.contrast).toBeGreaterThanOrEqual(4.5);
  expect(appearance.codeBackgroundContrast).toBeGreaterThanOrEqual(1.28);
  expect(appearance.codeBackgroundDelta).toBeGreaterThanOrEqual(24);
  expect(appearance.foreground).toEqual(appearance.expectedForeground);
});

test("local Markdown file link reveals its complete path without native title", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 480 });
  await page.goto("/tests/history-browser.html?local-file-link=1");
  const link = page.getByRole("button", {
    name: "在 Remote 中打开 /tmp/qwen3-tts-v017-release-test:42",
  });
  await expect(link).not.toHaveAttribute("title");

  await link.hover();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip).toContainText("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip).toBeInViewport();

  await tooltip.hover();
  await page.waitForTimeout(260);
  await expect(tooltip).toBeVisible();
  const path = tooltip.locator(".message-file-tooltip-path");
  await path.dblclick();
  await expect.poll(() => page.evaluate(() => getSelection()?.toString()))
    .toBe("/tmp/qwen3-tts-v017-release-test:42");
  await expect(tooltip.getByRole("button")).toHaveCount(0);

  await page.mouse.move(700, 460);
  await expect(tooltip).toHaveCount(0, { timeout: 1000 });
  await link.focus();
  await expect(page.getByRole("tooltip")).toBeVisible();
});

async function pinchThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  afterPinch: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const emit = (
      type: "pointerdown" | "pointermove" | "pointerup"
        | "lostpointercapture",
      pointerId: number,
      x: number,
      y: number,
    ) => stage.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      pointerId,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: type === "pointerup" || type === "lostpointercapture" ? 0 : 1,
    }));
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };

    emit("pointerdown", 1, 150, 260);
    emit("pointerdown", 2, 250, 260);
    emit("pointermove", 1, 50, 260);
    emit("pointermove", 2, 350, 260);
    await nextPaint();
    const afterPinch = transform();

    emit("lostpointercapture", 2, 350, 260);
    emit("pointermove", 1, 0, 300);
    await nextPaint();
    const afterPan = transform();
    emit("pointerup", 1, 50, 300);
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
    return { afterPinch, afterPan };
  });
}

async function wheelZoomThenPanPreview(
  page: import("@playwright/test").Page,
): Promise<{
  zoomPrevented: boolean;
  panPrevented: boolean;
  afterZoom: { scale: number; x: number; y: number };
  afterPan: { scale: number; x: number; y: number };
}> {
  return page.locator(".image-lightbox").evaluate(async (node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      clientWidth: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 720 },
    });
    Object.defineProperties(visual, {
      clientWidth: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 400 },
    });
    const nextPaint = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
    const transform = () => {
      const matrix = new DOMMatrix(visual.style.transform);
      return {
        x: matrix.e,
        y: matrix.f,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    };
    const zoomPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      clientX: 450,
      clientY: 360,
      deltaY: -600,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    const afterZoom = transform();
    const panPrevented = !stage.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: 450,
      clientY: 360,
      deltaX: 45,
      deltaY: 30,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
    }));
    await nextPaint();
    return {
      zoomPrevented,
      panPrevented,
      afterZoom,
      afterPan: transform(),
    };
  });
}

async function readingAnchor(page: import("@playwright/test").Page): Promise<{
  id: string;
  offset: number;
}> {
  return page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    if (!viewport) throw new Error("thread viewport is missing");
    const viewportRect = viewport.getBoundingClientRect();
    const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
      .map((row) => ({ row, rect: row.getBoundingClientRect() }))
      .filter(({ rect }) =>
        rect.bottom > viewportRect.top && rect.top < viewportRect.bottom);
    const selected = rows.sort((left, right) =>
      Math.abs(left.rect.top - viewportRect.top)
      - Math.abs(right.rect.top - viewportRect.top))[0];
    const id = selected?.row.dataset.turnId;
    if (!selected || !id) throw new Error("no visible reading anchor");
    return { id, offset: selected.rect.top - viewportRect.top };
  });
}

test("Codex settings opens the responsive daily usage activity view", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=codex");
  await page.getByRole("button", { name: "更多设置" }).click();
  const activityEntry = page.getByRole("button", { name: /使用活动/ });
  await expect(activityEntry).toBeVisible();
  await activityEntry.click();

  const dialog = page.getByRole("dialog", { name: "Codex 使用活动" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("累计 Token");
  await expect(dialog).toContainText("9.88亿");
  await expect(dialog).toContainText("单日峰值");
  await expect(dialog).toContainText("最长任务");
  await expect(dialog).toContainText("当前连续");
  await expect(dialog).toContainText("最长连续");
  await expect(dialog.locator(".usage-activity-tile")).toHaveCount(371);

  const geometry = await page.locator(".usage-activity-viewport")
    .evaluate((viewport) => ({
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      clientWidth: viewport.clientWidth,
      scrollWidth: viewport.scrollWidth,
      scrollLeft: viewport.scrollLeft,
    }));
  expect(geometry.pageScrollWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  if (geometry.scrollWidth > geometry.clientWidth) {
    expect(geometry.scrollLeft).toBeGreaterThan(0);
  }

  const busiest = dialog.locator('.usage-activity-tile[data-level="4"]')
    .first();
  await expect(busiest).toHaveAttribute(
    "title", /使用了 \d+(?:\.\d+)?万 个 Token/,
  );
  await busiest.click();
  await expect(dialog.locator(".usage-activity-caption"))
    .toContainText("Token");
});

test("Claude settings does not expose Codex usage activity", async ({ page }) => {
  await page.goto("/tests/history-browser.html?header-menu=1&engine=claude");
  await page.getByRole("button", { name: "更多设置" }).click();
  await expect(page.getByRole("button", { name: /使用活动/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /通知/ })).toBeVisible();
});

async function maxPaintedTurnOffsetShiftThroughAction(
  page: import("@playwright/test").Page,
  turnId: string,
  actionTestId: string,
  frameCount = 36,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, testId, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    const action = document.querySelector<HTMLElement>(
      `[data-testid="${CSS.escape(testId)}"]`,
    );
    if (!viewport || !row || !action) {
      throw new Error("frame sampling target is missing");
    }
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    action.click();
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      // rAF runs before ResizeObserver delivery. Sample after the paint
      // opportunity so the test observes user-visible frames, not the
      // browser's internal pre-observer layout checkpoint.
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return { maxShift, missing };
  }, { id: turnId, testId: actionTestId, frames: frameCount });
}

async function maxTurnOffsetShiftThroughTouchHistoryLoad(
  page: import("@playwright/test").Page,
  turnId: string,
  frameCount = 60,
): Promise<{ maxShift: number; missing: boolean }> {
  return page.evaluate(async ({ id, frames }) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) throw new Error("touch frame target is missing");
    const viewportTop = viewport.getBoundingClientRect().top;
    const initial = row.getBoundingClientRect().top - viewportTop;
    let maxShift = 0;
    let missing = false;
    const dispatch = (type: "touchstart" | "touchmove" | "touchend",
      clientY: number) => {
      const touch = {
        identifier: 1,
        target: viewport,
        clientX: 120,
        clientY,
      };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      viewport.dispatchEvent(event);
    };
    dispatch("touchstart", 160);
    dispatch("touchmove", 220);
    dispatch("touchend", 220);
    await new Promise<void>((resolve) => {
      let remaining = frames;
      const sample = () => {
        const current = document.querySelector<HTMLElement>(
          `[data-turn-id="${CSS.escape(id)}"]`,
        );
        if (!current) {
          missing = true;
        } else {
          const offset = current.getBoundingClientRect().top
            - viewport.getBoundingClientRect().top;
          maxShift = Math.max(maxShift, Math.abs(offset - initial));
        }
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });
    return { maxShift, missing };
  }, { id: turnId, frames: frameCount });
}

async function processDetailEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
): Promise<number> {
  return page.locator('[data-turn-id="detail-page"]').evaluate(
    (turn, selectedEdge) => {
      const viewport = document.querySelector<HTMLElement>(".thread");
      const process = turn.querySelector<HTMLElement>(
        "[data-process-detail-root]",
      );
      if (!viewport || !process) throw new Error("detail process is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const processRect = process.getBoundingClientRect();
      return (selectedEdge === "end" ? processRect.bottom : processRect.top)
        - viewportRect.top;
    },
    edge,
  );
}

async function turnIntersectsViewport(
  page: import("@playwright/test").Page,
  turnId: string,
): Promise<boolean> {
  return page.evaluate((id) => {
    const viewport = document.querySelector<HTMLElement>(".thread");
    const row = document.querySelector<HTMLElement>(
      `[data-turn-id="${CSS.escape(id)}"]`,
    );
    if (!viewport || !row) return false;
    const viewportRect = viewport.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    return rowRect.bottom > viewportRect.top && rowRect.top < viewportRect.bottom;
  }, turnId);
}

async function waitForScrollIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: number | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await page.locator(".thread").evaluate(
      (node) => node.scrollTop,
    );
    stableSamples = previous != null && Math.abs(current - previous) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread scroll position did not settle");
}

async function pauseOutputAndScrollToHistoryStart(
  page: import("@playwright/test").Page,
): Promise<void> {
  const viewport = page.locator(".thread");
  // Initial virtual measurements can finish one frame after the first write
  // and reassert the mounted tail. Retry the neutral test setup until the
  // physical viewport itself is stable at the history edge; no user-intent
  // event is emitted here, so the page request still belongs to the action
  // under test.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await viewport.evaluate((node) => { node.scrollTop = 0; });
    await waitForScrollIdle(page);
    if (await viewport.evaluate((node) => node.scrollTop <= 1)) return;
  }
  expect(await viewport.evaluate((node) => node.scrollTop)).toBeLessThanOrEqual(1);
}

async function waitForReadingPositionIdle(
  page: import("@playwright/test").Page,
): Promise<void> {
  let previous: { id: string; offset: number } | null = null;
  let stableSamples = 0;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = await readingAnchor(page).catch(() => null);
    if (!current) {
      previous = null;
      stableSamples = 0;
      await page.waitForTimeout(50);
      continue;
    }
    stableSamples = previous != null
      && current.id === previous.id
      && Math.abs(current.offset - previous.offset) < 0.5
      ? stableSamples + 1 : 0;
    if (stableSamples >= 4) return;
    previous = current;
    await page.waitForTimeout(50);
  }
  throw new Error("thread visual reading position did not settle");
}

async function nativeSelectionSnapshot(
  page: import("@playwright/test").Page,
): Promise<{
  anchorTurnId: string | null;
  focusTurnId: string | null;
  anchorConnected: boolean;
  text: string;
}> {
  return page.evaluate(() => {
    const selection = window.getSelection();
    const turnId = (node: Node | null): string | null => {
      const element = node instanceof Element ? node : node?.parentElement;
      return element?.closest<HTMLElement>("[data-turn-id]")
        ?.dataset.turnId ?? null;
    };
    return {
      anchorTurnId: turnId(selection?.anchorNode ?? null),
      focusTurnId: turnId(selection?.focusNode ?? null),
      anchorConnected: selection?.anchorNode?.isConnected ?? false,
      text: selection?.toString() ?? "",
    };
  });
}

async function textSelectionPoint(
  locator: import("@playwright/test").Locator,
): Promise<{ x: number; y: number }> {
  const point = await locator.evaluate((node) => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const text = walker.nextNode();
    if (!text || !text.textContent?.length) return null;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, Math.min(2, text.textContent.length));
    const rect = range.getBoundingClientRect();
    return { x: rect.left + 1, y: rect.top + rect.height / 2 };
  });
  if (!point) throw new Error("selection text has no geometry");
  return point;
}

function isMobileWebKitProject(projectName: string): boolean {
  return projectName.startsWith("webkit");
}

async function dispatchCancelledTouchTap(
  locator: import("@playwright/test").Locator,
  pointerId: number,
): Promise<void> {
  await locator.evaluate((node, id) => {
    const target = node as HTMLElement;
    let capturedPointer: number | null = null;
    let releases = 0;
    Object.defineProperties(target, {
      setPointerCapture: {
        configurable: true,
        value: (candidate: number) => { capturedPointer = candidate; },
      },
      hasPointerCapture: {
        configurable: true,
        value: (candidate: number) => capturedPointer === candidate,
      },
      releasePointerCapture: {
        configurable: true,
        value: (candidate: number) => {
          if (capturedPointer === candidate) capturedPointer = null;
          releases += 1;
        },
      },
    });
    target.focus();
    const bounds = target.getBoundingClientRect();
    const clientX = bounds.left + bounds.width / 2;
    const clientY = bounds.top + bounds.height / 2;
    target.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
      buttons: 1,
    }));
    target.dispatchEvent(new PointerEvent("pointercancel", {
      bubbles: true,
      cancelable: true,
      pointerId: id,
      pointerType: "touch",
      clientX,
      clientY,
    }));
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX,
      clientY,
      detail: 1,
    }));
    target.dataset.cancelReleaseCount = String(releases);
    target.dataset.cancelCaptureActive = String(capturedPointer === id);
    target.dataset.cancelStillFocused = String(document.activeElement === target);
  }, pointerId);
  await expect(locator).toHaveAttribute("data-cancel-release-count", "1");
  await expect(locator).toHaveAttribute("data-cancel-capture-active", "false");
  await expect(locator).toHaveAttribute("data-cancel-still-focused", "false");
}

async function wheelUntilTurn(
  page: import("@playwright/test").Page,
  turnId: string,
  deltaY: number,
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (isMobileWebKitProject(projectName)) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      if (await turnIntersectsViewport(page, turnId)) {
        if (deltaY < 0) {
          await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
        }
        return;
      }
      await dispatchTouchGesture(page, deltaY < 0 ? 60 : -60);
      await viewport.evaluate((node, delta) => {
        const step = Math.max(
          160,
          Math.min(Math.abs(delta), node.clientHeight * 5),
        );
        node.scrollBy({
          top: Math.sign(delta) * step,
          behavior: "auto",
        });
      }, deltaY);
      await waitForScrollIdle(page);
    }
    expect(await turnIntersectsViewport(page, turnId)).toBe(true);
    return;
  }
  const box = await viewport.boundingBox();
  if (!box) throw new Error("thread viewport has no bounds");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (await turnIntersectsViewport(page, turnId)) return;
    await page.mouse.wheel(0, deltaY);
    await page.waitForTimeout(40);
  }
  expect(await turnIntersectsViewport(page, turnId)).toBe(true);
}

async function scrollThreadToEdge(
  page: import("@playwright/test").Page,
  edge: "start" | "end",
  projectName: string,
): Promise<void> {
  const viewport = page.locator(".thread");
  for (let attempt = 0; attempt < 6; attempt += 1) {
    if (isMobileWebKitProject(projectName)) {
      await dispatchTouchGesture(page, edge === "start" ? 60 : -60);
    } else {
      await viewport.dispatchEvent("wheel", {
        deltaY: edge === "start" ? -80 : 80,
      });
    }
    await viewport.evaluate((node, selectedEdge) => {
      node.scrollTo({
        top: selectedEdge === "start" ? 0 : node.scrollHeight,
        behavior: "auto",
      });
      // Synthetic touch events mark genuine reader intent, but Playwright
      // WebKit does not perform the browser's native pan for them. Deliver the
      // matching scroll notification after the fixture's explicit scrollTop
      // write so React and the virtualizer observe the same offset before the
      // next dynamic row measurement.
      node.dispatchEvent(new Event("scroll"));
    }, edge);
    await waitForScrollIdle(page);
    const reached = await viewport.evaluate((node, selectedEdge) => {
      if (selectedEdge === "start") return node.scrollTop <= 1;
      return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
    }, edge);
    if (reached) {
      if (edge === "start") {
        await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
      }
      return;
    }
  }
  expect(await viewport.evaluate((node, selectedEdge) => {
    if (selectedEdge === "start") return node.scrollTop <= 1;
    return node.scrollHeight - node.scrollTop - node.clientHeight <= 1;
  }, edge)).toBe(true);
}

async function dispatchTouchGesture(
  page: import("@playwright/test").Page,
  fingerDeltaY: number,
  moves = 1,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const dispatchTouch = (
      type: "touchstart" | "touchmove" | "touchend",
      clientY: number,
    ) => {
      // WebKit's Touch constructor is intentionally not public. React only
      // needs the TouchEvent list shape, so define it on a real bubbling Event.
      const touch = { identifier: 1, target, clientX: 120, clientY };
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        touches: { value: type === "touchend" ? [] : [touch] },
        targetTouches: { value: type === "touchend" ? [] : [touch] },
        changedTouches: { value: [touch] },
      });
      target.dispatchEvent(event);
    };
    const startY = 160;
    dispatchTouch("touchstart", startY);
    for (let index = 0; index < input.moves; index += 1) {
      dispatchTouch("touchmove", startY + input.fingerDeltaY * (index + 1));
    }
    dispatchTouch("touchend", startY + input.fingerDeltaY * input.moves);
  }, { fingerDeltaY, moves });
}

async function dispatchTouchPhase(
  page: import("@playwright/test").Page,
  type: "touchstart" | "touchmove" | "touchend",
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement;
    const touch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.clientY,
    };
    const event = new Event(input.type, { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: input.type === "touchend" ? [] : [touch] },
      targetTouches: { value: input.type === "touchend" ? [] : [touch] },
      changedTouches: { value: [touch] },
    });
    target.dispatchEvent(event);
  }, { type, clientY });
}

async function stageDelayedTouchMove(
  page: import("@playwright/test").Page,
  startY: number,
  clientY: number,
): Promise<void> {
  await page.locator(".thread").evaluate((node, input) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const startTouch = {
      identifier: 1,
      target,
      clientX: 120,
      clientY: input.startY,
    };
    const start = new Event("touchstart", { bubbles: true, cancelable: true });
    Object.defineProperties(start, {
      touches: { value: [startTouch] },
      targetTouches: { value: [startTouch] },
      changedTouches: { value: [startTouch] },
    });
    target.dispatchEvent(start);
    const touch = { ...startTouch, clientY: input.clientY };
    const event = new Event("touchmove", { bubbles: true, cancelable: true });
    Object.defineProperties(event, {
      touches: { value: [touch] },
      targetTouches: { value: [touch] },
      changedTouches: { value: [touch] },
    });
    target.__delayedTouchMove = event;
  }, { startY, clientY });
}

async function dispatchDelayedTouchMove(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".thread").evaluate((node) => {
    const target = node as HTMLElement & { __delayedTouchMove?: Event };
    const event = target.__delayedTouchMove;
    delete target.__delayedTouchMove;
    if (!event) throw new Error("delayed touchmove was not staged");
    target.dispatchEvent(event);
  });
}

async function requestOlderHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (!isMobileWebKitProject(projectName)) {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: -80 });
    }
    return;
  }
  await dispatchTouchGesture(page, 60, repeat);
}

async function requestNewerHistory(
  page: import("@playwright/test").Page,
  projectName: string,
  repeat = 1,
): Promise<void> {
  const viewport = page.locator(".thread");
  if (!isMobileWebKitProject(projectName)) {
    for (let index = 0; index < repeat; index += 1) {
      await viewport.dispatchEvent("wheel", { deltaY: 80 });
    }
    return;
  }
  await dispatchTouchGesture(page, -60, repeat);
}

test("prepend preserves the exact reading row through delayed row growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="o1"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.locator('[data-turn-id="n8"]')).toBeVisible();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  await page.waitForTimeout(800);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("reducer history paging and live refresh keep one stable projection", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");

  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  await page.getByRole("button", { name: "加载更早的历史" }).click();
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A same-revision newest-page refresh is delivered to the authoritative
  // runtime while the reducer-owned browse projection remains visible.
  await page.getByTestId("reducer-live-refresh").click();
  await expect(page.getByTestId("reducer-refresh-count")).toHaveText("1");
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("40");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("40");
  const afterRefresh = await readingAnchor(page);
  expect(afterRefresh.id).toBe(before.id);
  expect(Math.abs(afterRefresh.offset - before.offset)).toBeLessThan(2);

  // A -> B -> A discards only the display browse window. The accepted runtime
  // must still contain one canonical copy of every newest turn.
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-b");
  await expect(page.locator('[data-turn-id="reducer-b8"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.getByTestId("reducer-focused-sid"))
    .toHaveText("reducer-history-session-a");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await expect(page.getByTestId("reducer-turn-count")).toHaveText("20");
  await expect(page.getByTestId("reducer-unique-turn-count")).toHaveText("20");
});

test("authoritative paging returns after an IndexedDB first paint", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?reducer-pipeline=1&cached-paging=1",
  );
  await expect(page.locator('[data-turn-id="reducer-cached-current"]'))
    .toBeVisible();
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const loader = page.getByTestId("load-older-history");
  await expect(loader).toBeVisible();
  const [loaderBox, viewportBox] = await Promise.all([
    loader.boundingBox(),
    viewport.boundingBox(),
  ]);
  expect(loaderBox).not.toBeNull();
  expect(viewportBox).not.toBeNull();
  expect(loaderBox!.y).toBeGreaterThanOrEqual(viewportBox!.y);
  expect(loaderBox!.y + loaderBox!.height)
    .toBeLessThanOrEqual(viewportBox!.y + viewportBox!.height);
});

test("reducer prepend never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "load-older-history",
    60,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("touching the history edge never paints an intermediate reading-row jump", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?reducer-pipeline=1");
  await expect(page.locator('[data-turn-id="reducer-m40"]')).toBeVisible();
  await pauseOutputAndScrollToHistoryStart(page);
  const before = await readingAnchor(page);
  const sampled = await maxTurnOffsetShiftThroughTouchHistoryLoad(
    page,
    before.id,
  );
  await expect(page.locator('[data-turn-id="reducer-m20"]')).toBeAttached();
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a history page waits for the active touch to release before mounting", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?delay=20&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  const pageActivity = page.getByTestId("history-page-activity");
  await expect(pageActivity).toContainText("正在加载更早历史");
  await expect(pageActivity).toHaveCSS("pointer-events", "none");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(pageActivity).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(pageActivity).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("the first runtime browse page stays staged under an active touch", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=0&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(100);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  const held = await readingAnchor(page);
  expect(held.id).toBe(before.id);
  expect(Math.abs(held.offset - before.offset)).toBeLessThan(2);

  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("the first runtime browse page stays staged until wheel idle", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=20&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await viewport.evaluate((node) => {
    const target = node as HTMLElement & { __wheelLease?: number };
    const signalWheel = () => target.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    signalWheel();
    target.__wheelLease = window.setInterval(signalWheel, 50);
  });
  try {
    await expect(page.getByTestId("load-count")).toHaveText("1");
    await page.waitForTimeout(100);
    await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
    await expect(page.getByTestId("history-page-activity")).toBeVisible();
  } finally {
    await viewport.evaluate((node) => {
      const target = node as HTMLElement & { __wheelLease?: number };
      if (target.__wheelLease != null) {
        window.clearInterval(target.__wheelLease);
        delete target.__wheelLease;
      }
    });
  }

  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("older history becoming available during a wheel gesture is restored once", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop wheel path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await viewport.dispatchEvent("wheel", { deltaY: -80 });

  const pending = await readingAnchor(page);
  expect(pending.id).toBe(before.id);
  expect(Math.abs(pending.offset - before.offset)).toBeLessThan(2);
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await viewport.evaluate((node) => {
    node.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      deltaY: -80,
    }));
    const reveal = document.querySelector<HTMLButtonElement>(
      '[data-testid="reveal-older-history"]',
    );
    if (!reveal) throw new Error("history reveal control is missing");
    reveal.click();
  });
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("older history becoming available under touch waits for release", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "mobile WebKit touch path");
  await page.goto(
    "/tests/history-browser.html?delayed-history-availability=1&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await page.getByTestId("reveal-older-history").click();

  await expect(page.getByText("正在恢复历史…")).toBeVisible();
  await expect(page.getByTestId("load-count")).toHaveText("0");
  await dispatchTouchPhase(page, "touchend", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await page.waitForTimeout(250);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("one click loads every turn-detail page without collapsing or jumping", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=1000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const initialStart = await processDetailEdge(page, "start");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("正在加载过程…")).toBeVisible();
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect(page.getByRole("button", { name: "加载更早过程" }))
    .toHaveCount(0);
  await expect(page.getByRole("button", { name: "返回较新过程" }))
    .toHaveCount(0);
  await page.waitForTimeout(500);
  expect(Math.abs(await processDetailEdge(page, "start") - initialStart))
    .toBeLessThan(2);
  await expect(page.locator(".thread"))
    .toHaveAttribute("data-detail-anchor-active", "false");
});

test("a loading process can collapse and reopen without issuing a duplicate read", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&delay=10000&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  expect(await page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("1");
});

test("a failed process detail stays open and retries in place", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-error-once=1"
      + "&delay=500&growth-delay=30",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await page.getByRole("button", { name: "重试" }).click();
  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toBeVisible();
});

test("a failed older process page retries the exact cursor in place", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-older-error-once=1"
      + "&delay=30&growth-delay=10000",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "详细过程暂时不可用，请稍后重试",
  );
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.getByText("较早命令 1")).toBeVisible();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailRequests,
  )).toBe("3");
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.detailLastBefore,
  )).toBe("detail-older");
});

test("retained truncated process still fetches its authoritative first detail page", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-retained-preview=1"
      + "&delay=500&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.click();
  await expect(page.getByText("已缓存的较新命令")).toBeVisible();
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(header).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("较早过程已省略")).toHaveCount(0);
  await expect(page.getByText("较新命令 1")).toBeVisible();
  await expect(page.getByText("较早命令 1")).toBeVisible();
});

test("user scrolling cancels a pending turn-detail anchor", async ({ page }) => {
  await page.goto(
    "/tests/history-browser.html?detail-paging=1&detail-scroll-cancel=1"
      + "&delay=250&growth-delay=180",
  );
  const header = page.locator(".turn-process-head");
  await header.scrollIntoViewIfNeeded();
  await header.click();
  await page.waitForTimeout(40);
  const viewport = page.locator(".thread");
  await viewport.dispatchEvent("wheel", { deltaY: 90 });
  await viewport.evaluate((node) => { node.scrollTop += 90; });
  await page.waitForTimeout(40);
  const userOffset = await processDetailEdge(page, "start");

  await expect(page.getByText("较早命令 1")).toBeVisible();
  await page.waitForTimeout(450);
  expect(Math.abs(await processDetailEdge(page, "start") - userOffset))
    .toBeLessThan(2);
});

test("history page cache rebuilds a v1 record in real IndexedDB", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/history-page-cache.ts";
    const cacheModule = await import(modulePath);
    const scope = {
      machineId: "browser-machine",
      engine: "codex",
      space: "code",
      sid: "browser-cache-session",
      revision: "browser-cache-revision",
    };
    const pageKey = "v1-page";
    const key = cacheModule.historyPageCachePageKey(scope, pageKey);
    const scopeKey = cacheModule.historyPageCacheScopeKey(scope);
    const sessionKey = cacheModule.historyPageCacheSessionKey(scope);
    const legacyDb = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(cacheModule.HISTORY_PAGE_CACHE_DB_NAME, 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore("pages", {
          keyPath: "key",
        });
        store.createIndex("scope", "scopeKey", { unique: false });
        store.createIndex("session", "sessionKey", { unique: false });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = legacyDb.transaction("pages", "readwrite");
      transaction.objectStore("pages").put({
        version: 1,
        key,
        scopeKey,
        sessionKey,
        ...scope,
        pageKey,
        page: {
          pageKey,
          turns: [{
            id: "legacy-turn",
            prompt: "legacy",
            blocks: [],
            done: true,
          }],
          hasOlder: false,
          olderCursor: "legacy-turn",
          hasNewer: false,
          newerPageKey: null,
          isLatest: false,
        },
        savedAt: 1,
        byteSize: 256,
      });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    legacyDb.close();

    const cache = new cacheModule.HistoryPageCache();
    const upgraded = await cache.getPage(scope, pageKey);
    const stored = await cache.putPage(scope, {
      pageKey,
      turns: [{
        id: "new-turn",
        prompt: "new",
        blocks: [],
        done: true,
      }],
      hasOlder: false,
      olderCursor: "legacy-turn",
    });
    const merged = await cache.getPage(scope, pageKey);
    const invalidated = await cache.invalidateScope(scope);
    const afterInvalidation = await cache.getPage(scope, pageKey);
    return {
      legacyMiss: upgraded === null,
      stored,
      mergedIds: merged?.turns.map((turn: { id: string }) => turn.id),
      invalidated,
      afterInvalidation,
    };
  });
  expect(result.legacyMiss).toBe(true);
  expect(result.stored.ok).toBe(true);
  expect(result.mergedIds).toEqual(["new-turn"]);
  expect(result.invalidated.ok).toBe(true);
  expect(result.afterInvalidation).toBeNull();
});

test("instant session cache preserves a heavy turn's complete process skeleton", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    cache.saveSession("heavy-refresh-session", [{
      id: "heavy-refresh-turn",
      prompt: "1",
      done: true,
      images: [{
        media_type: "image/png",
        data: "i".repeat(2 * 1024 * 1024 + 1),
      }],
      blocks: [
        {
          kind: "text", message_id: "heavy-commentary", text: "2",
          done: true, channel: "commentary",
        },
        {
          kind: "tool", message_id: "heavy-tool-message-3",
          tool_use_id: "heavy-tool-3", tool: "Read",
          input: { file_path: "/tmp/3" },
          result: {
            content: "x".repeat(2 * 1024 * 1024 + 1),
            is_error: false,
          },
          done: true,
        },
        {
          kind: "tool", message_id: "heavy-tool-message-4",
          tool_use_id: "heavy-tool-4", tool: "Bash",
          input: { command: "echo 4" }, done: true,
        },
        {
          kind: "process", item_id: "heavy-process-5",
          processKind: "command", phase: "snapshot", status: "succeeded",
          title: "5", done: true,
        },
        {
          kind: "text", message_id: "heavy-final", text: "6",
          done: true, channel: "final",
        },
      ],
      detailEventCount: 4,
    }], 42, "heavy-refresh-r1", "heavy-refresh-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const loaded = await cache.loadSession("heavy-refresh-session");
    const turn = loaded?.turns[0] as {
      images?: unknown[];
      blocks?: Array<Record<string, unknown>>;
      detailProjection?: unknown;
    } | undefined;
    return {
      size: new TextEncoder().encode(JSON.stringify(turn)).byteLength,
      hasImages: Array.isArray(turn?.images),
      hasDetailProjection: turn?.detailProjection != null,
      process: turn?.blocks?.map((block) => (
        block.kind === "text" ? block.text
          : block.kind === "tool" ? block.tool_use_id
            : block.title
      )),
    };
  });
  expect(result.size).toBeLessThan(2 * 1024 * 1024);
  expect(result.hasImages).toBe(false);
  expect(result.hasDetailProjection).toBe(false);
  expect(result.process).toEqual([
    "2", "heavy-tool-3", "heavy-tool-4", "5", "6",
  ]);
});

test("instant session cache persists a bounded history-start proof only", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    const turn = (id: string) => ({
      id, prompt: id, blocks: [], done: true,
    });
    cache.saveSession(
      "complete-history-head",
      [turn("complete-turn")],
      0,
      "complete-history-r1",
      "complete-history-g1",
      null,
      true,
    );
    cache.saveSession(
      "trimmed-history-head",
      Array.from({ length: 101 }, (_, index) => turn(`trimmed-${index}`)),
      0,
      "trimmed-history-r1",
      "trimmed-history-g1",
      null,
      true,
    );
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const complete = await cache.loadSession("complete-history-head");
    const trimmed = await cache.loadSession("trimmed-history-head");
    return {
      completeAtStart: complete?.historyAtStart,
      trimmedAtStart: trimmed?.historyAtStart,
      trimmedTurns: trimmed?.turns.length,
      pagingKeys: complete == null
        ? []
        : ["hasMore", "oldestId", "cursor"].filter((key) => (
            Object.prototype.hasOwnProperty.call(complete, key)
          )),
    };
  });
  expect(result.completeAtStart).toBe(true);
  expect(result.trimmedAtStart).toBe(false);
  expect(result.trimmedTurns).toBe(100);
  expect(result.pagingKeys).toEqual([]);
});

test("session cache rejects stale Claude and replay-orphan rows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html");
  const result = await page.evaluate(async () => {
    const cache = await import("/src/cache.ts");
    await cache.clearCache();
    const legacySid = "legacy-claude-prompt-alias";
    const replayOrphanSid = "completed-replay-orphan";
    const activeCompactionOrphanSid = "active-compaction-replay-orphan";
    const recoveredOwnerV16Sid = "completed-recovery-owner-v16";
    const lateSeedV17Sid = "active-late-binding-seed-v17";
    const optimisticSteerSid = "healthy-optimistic-steer";
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("cc_remote_cache", 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const tx = database.transaction("sessions", "readwrite");
      tx.objectStore("sessions").put({
        v: 10,
        turns: [{
          id: "claude-transcript-uuid",
          prompt: "legacy prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 41,
        revision: "legacy-r1",
        generation: "legacy-g1",
        savedAt: Date.now(),
      }, legacySid);
      tx.objectStore("sessions").put({
        v: 13,
        turns: [{
          id: "native-history-turn",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "replayed-assistant-message",
          prompt: "",
          blocks: [{
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-a",
            tool: "Command",
            input: {},
            done: true,
          }, {
            kind: "tool",
            message_id: "replayed-assistant-message",
            tool_use_id: "replayed-tool-b",
            tool: "Command",
            input: {},
            done: true,
          }],
          done: true,
        }],
        lastSeq: 43,
        revision: "replay-orphan-r1",
        generation: "replay-orphan-g1",
        savedAt: Date.now(),
      }, replayOrphanSid);
      tx.objectStore("sessions").put({
        v: 16,
        turns: [{
          id: "browser-owner",
          clientMsgId: "browser-owner",
          forkPointId: "native-owner",
          prompt: "deploy",
          blocks: [],
          done: true,
        }, {
          id: "recovered-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "recovered-answer",
            text: "done",
            channel: "final",
            done: true,
          }],
          done: true,
        }],
        lastSeq: 365,
        revision: "recovered-owner-r1",
        generation: "recovered-owner-g1",
        savedAt: Date.now(),
      }, recoveredOwnerV16Sid);
      tx.objectStore("sessions").put({
        v: 15,
        turns: [{
          id: "item-51",
          prompt: "continue the task",
          forkPointId: "native-turn",
          blocks: [{
            kind: "process",
            item_id: "item-54",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }, {
          id: "msg-after-compact",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "msg-after-compact",
            text: "continued output",
            channel: "commentary",
            done: true,
          }, {
            kind: "process",
            item_id: "replayed-compaction",
            processKind: "compaction",
            phase: "snapshot",
            status: "succeeded",
            turn_id: "native-turn",
            title: "压缩上下文",
            done: true,
          }],
          done: false,
        }],
        lastSeq: 364,
        revision: "active-compaction-r1",
        generation: "active-compaction-g1",
        savedAt: Date.now(),
      }, activeCompactionOrphanSid);
      tx.objectStore("sessions").put({
        v: 17,
        turns: [{
          id: "canonical-current-owner",
          clientMsgId: "canonical-current-owner",
          forkPointId: "shared-current-native-turn",
          prompt: "current prompt",
          blocks: [],
          done: false,
        }, {
          id: "late-seeded-live-tail",
          prompt: "",
          blocks: [{
            kind: "text",
            message_id: "late-seeded-live-tail",
            text: "duplicated current suffix",
            channel: "commentary",
            done: false,
          }],
          done: false,
        }],
        lastSeq: 46,
        revision: "late-seed-r1",
        generation: "late-seed-g1",
        savedAt: Date.now(),
      }, lateSeedV17Sid);
      tx.objectStore("sessions").put({
        v: 20,
        turns: [{
          id: "active-before-steer",
          prompt: "first prompt",
          forkPointId: "shared-native-turn",
          blocks: [],
          done: false,
        }, {
          id: "optimistic-steer",
          clientMsgId: "optimistic-steer",
          prompt: "second prompt",
          blocks: [],
          done: false,
        }],
        lastSeq: 47,
        revision: "optimistic-steer-r1",
        generation: "optimistic-steer-g1",
        savedAt: Date.now(),
      }, optimisticSteerSid);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    const legacy = await cache.loadSession(legacySid);
    const replayOrphan = await cache.loadSession(replayOrphanSid);
    const recoveredOwnerV16 = await cache.loadSession(recoveredOwnerV16Sid);
    const activeCompactionOrphan = await cache.loadSession(
      activeCompactionOrphanSid);
    const lateSeedV17 = await cache.loadSession(lateSeedV17Sid);
    const optimisticSteer = await cache.loadSession(optimisticSteerSid);
    const replay = await cache.loadAllReplayState();
    await new Promise((resolve) => window.setTimeout(resolve, 100));
    const prunedCompactionOrphan = await new Promise((resolve, reject) => {
      const tx = database.transaction("sessions", "readonly");
      const request = tx.objectStore("sessions").get(activeCompactionOrphanSid);
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => reject(request.error);
    });
    cache.saveSession("current-claude-prompt-alias", [{
      id: "browser-prompt-id",
      clientMsgId: "browser-prompt-id",
      historyTurnId: "claude-transcript-uuid",
      prompt: "current prompt",
      blocks: [],
      done: false,
    }], 42, "current-r1", "current-g1");
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const current = await cache.loadSession("current-claude-prompt-alias");
    database.close();
    return {
      legacy,
      legacyCursor: replay.cursors[legacySid],
      replayOrphan,
      replayOrphanCursor: replay.cursors[replayOrphanSid],
      recoveredOwnerV16,
      recoveredOwnerV16Cursor: replay.cursors[recoveredOwnerV16Sid],
      activeCompactionOrphan,
      activeCompactionCursor: replay.cursors[activeCompactionOrphanSid],
      prunedCompactionOrphan,
      lateSeedV17,
      lateSeedV17Cursor: replay.cursors[lateSeedV17Sid],
      optimisticSteerCount: optimisticSteer?.turns.length,
      optimisticSteerCursor: replay.cursors[optimisticSteerSid],
      currentIds: current?.turns.map((turn: {
        id: string; clientMsgId?: string; historyTurnId?: string;
      }) => [turn.id, turn.clientMsgId, turn.historyTurnId]),
    };
  });
  expect(result.legacy).toBeNull();
  expect(result.legacyCursor).toBeUndefined();
  expect(result.replayOrphan).toBeNull();
  expect(result.replayOrphanCursor).toBeUndefined();
  expect(result.recoveredOwnerV16).toBeNull();
  expect(result.recoveredOwnerV16Cursor).toBeUndefined();
  expect(result.activeCompactionOrphan).toBeNull();
  expect(result.activeCompactionCursor).toBeUndefined();
  expect(result.prunedCompactionOrphan).toBeNull();
  expect(result.lateSeedV17).toBeNull();
  expect(result.lateSeedV17Cursor).toBeUndefined();
  expect(result.optimisticSteerCount).toBe(2);
  expect(result.optimisticSteerCursor).toBe(47);
  expect(result.currentIds).toEqual([[
    "browser-prompt-id", "browser-prompt-id", "claude-transcript-uuid",
  ]]);
});

test("a canonical image reference does not reserve a second hidden image row", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  const assertCanonicalImageLayout = async () => {
    const turn = page.locator('[data-turn-id="dual-image"]');
    await expect(turn).toBeVisible();
    await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
    const gap = await turn.evaluate((node) => {
      const image = node.querySelector<HTMLElement>(".ubub-image-trigger");
      const meta = node.querySelector<HTMLElement>(".ubub-meta");
      if (!image || !meta) throw new Error("image layout is incomplete");
      return meta.getBoundingClientRect().top - image.getBoundingClientRect().bottom;
    });
    expect(gap).toBeLessThan(20);
  };

  await assertCanonicalImageLayout();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.getByTestId("switch-session").click();
  await assertCanonicalImageLayout();
});

test("fallback image preview and canonical retry remain independent", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?history-image-fallback-error=1");
  const turn = page.locator('[data-turn-id="history-fallback-error"]');
  const imageRow = turn.locator(".ubub-imgs");
  const preview = turn.getByRole("button", {
    name: "预览用户发送的图片",
  });
  const retry = turn.getByRole("button", { name: "点击重试" });

  await expect(turn).toBeVisible();
  await expect(imageRow).toHaveCount(1);
  await expect(imageRow.locator(":scope > .history-image-control"))
    .toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
  await expect(preview).toBeVisible();
  await expect(retry).toBeVisible();
  const gap = await turn.evaluate((node) => {
    const row = node.querySelector<HTMLElement>(".ubub-imgs");
    const meta = node.querySelector<HTMLElement>(".ubub-meta");
    if (!row || !meta) throw new Error("canonical image layout is incomplete");
    return meta.getBoundingClientRect().top - row.getBoundingClientRect().bottom;
  });
  expect(gap).toBeLessThan(20);
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("0");
  await page.locator(".image-lightbox-close").click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);

  await retry.click();
  await expect(page.getByTestId("history-fallback-loads")).toHaveText("1");
  await expect(page.getByTestId("history-fallback-last-load"))
    .toHaveText("history-fallback-error|history-fallback-image|thumbnail");
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(imageRow).toHaveCount(1);
  await expect(turn.locator(".ubub-image-trigger")).toHaveCount(1);
});

test("streaming rerenders cannot cancel an image preview close", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?dual-image=1");
  await page.locator(".ubub-image-trigger").click();
  await expect(page.locator(".image-lightbox")).toBeVisible();

  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>(".image-lightbox-close")?.click();
    document.querySelector<HTMLButtonElement>('[data-testid="append-turn"]')?.click();
  });

  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("expanded tool batches use dense rows instead of individual cards", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  await page.locator(".tool-group-h").click();

  const rows = page.locator(".tool-group-b .tool");
  await expect(rows).toHaveCount(3);
  const styles = await rows.evaluateAll((nodes) => nodes.map((node) => {
    const style = getComputedStyle(node);
    return {
      height: node.getBoundingClientRect().height,
      border: style.borderTopWidth,
      radius: style.borderTopLeftRadius,
      shadow: style.boxShadow,
    };
  }));
  for (const style of styles) {
    expect(style.height).toBeLessThan(40);
    expect(style.border).toBe("0px");
    expect(style.radius).toBe("0px");
    expect(style.shadow).toBe("none");
  }
});

test("tool disclosures keep keyboard activation and ignore a scroll drag", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  const firstToolSummary = firstTool.locator(":scope > summary");
  await firstToolSummary.focus();
  await page.keyboard.press("Enter");
  await expect(firstTool).toHaveAttribute("open", "");
  await page.keyboard.press("Enter");
  await expect(firstTool).not.toHaveAttribute("open", "");

  await groupSummary.focus();
  await page.keyboard.press("Enter");
  await expect(group).not.toHaveAttribute("open", "");
  const box = await groupSummary.boundingBox();
  if (!box) throw new Error("tool group summary has no geometry");
  await page.mouse.move(box.x + 20, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y + box.height / 2 + 24, {
    steps: 4,
  });
  await page.mouse.up();
  await expect(group).not.toHaveAttribute("open", "");
});

test("iOS pointercancel releases tool disclosures without toggling", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?compact-tools=1");
  await page.locator(".turn-process-head").click();
  const group = page.locator("details.tool-group");
  const groupSummary = group.locator(":scope > summary");

  await dispatchCancelledTouchTap(groupSummary, 181);
  await expect(group).not.toHaveAttribute("open", "");

  await groupSummary.click();
  await expect(group).toHaveAttribute("open", "");
  const firstTool = group.locator("details.tool").first();
  await dispatchCancelledTouchTap(
    firstTool.locator(":scope > summary"), 182,
  );
  await expect(firstTool).not.toHaveAttribute("open", "");
});

test("completed Mermaid fences render isolated sanitized SVGs", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagrams = page.locator(".mermaid-block");
  await expect(diagrams).toHaveCount(2);
  await expect(page.locator(".mermaid-svg")).toHaveCount(2);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
  await expect(diagrams.nth(1)).toHaveAttribute("data-mermaid-state", "ready");

  const rootIds = await page.locator(".mermaid-svg > svg").evaluateAll(
    (nodes) => nodes.map((node) => node.id),
  );
  expect(rootIds.every(Boolean)).toBe(true);
  expect(new Set(rootIds).size).toBe(rootIds.length);
  await expect(page.locator(
    ".mermaid-svg script, .mermaid-svg foreignObject, .mermaid-svg image, "
      + ".mermaid-svg a",
  )).toHaveCount(0);
  const unsafeReferences = await page.locator(".mermaid-svg svg").evaluateAll(
    (nodes) => nodes.flatMap((node) =>
      [...node.querySelectorAll("*")].flatMap((element) =>
        [...element.attributes]
          .filter((attribute) => ["href", "xlink:href", "src"].includes(
            attribute.name.toLowerCase(),
          ))
          .map((attribute) => attribute.value)
          .filter((value) => !value.startsWith("#")))),
  );
  expect(unsafeReferences).toEqual([]);
  const clippedNodes = await diagrams.nth(0).evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("flowchart SVG is missing");
    const bounds = svg.getBoundingClientRect();
    return [...svg.querySelectorAll<SVGGraphicsElement>(".node")].filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
        || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1;
    }).length;
  });
  expect(clippedNodes).toBe(0);

  for (const diagram of await diagrams.all()) {
    const sizes = await diagram.evaluate((node) => {
      const svg = node.querySelector("svg");
      if (!svg) throw new Error("rendered Mermaid is missing its SVG");
      return {
        container: node.getBoundingClientRect().width,
        svg: svg.getBoundingClientRect().width,
      };
    });
    expect(sizes.svg).toBeLessThanOrEqual(sizes.container + 1);
  }

  const lightId = await page.locator(".mermaid-svg > svg").first().getAttribute("id");
  await page.evaluate(() => {
    document.documentElement.dataset.theme = "dark";
  });
  await expect.poll(async () =>
    page.locator(".mermaid-svg > svg").first().getAttribute("id"),
  ).not.toBe(lightId);
  await expect(diagrams.nth(0)).toHaveAttribute("data-mermaid-state", "ready");
});

test("completed chat formulas lazy-load accessible KaTeX markup", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?math=1");
  await expect(page.locator(".katex-display")).toHaveCount(1);
  await expect(page.locator(".katex")).toHaveCount(2);
  await expect(page.locator(".katex-mathml math")).toHaveCount(2);
  await expect(page.locator('[data-turn-id="math"]')).toContainText(
    "Inline:",
  );
  await expect(page.locator('[data-turn-id="math"] .message-code-copy'))
    .toHaveCount(0);
});

test("a completed streaming formula renders while following the live tail", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(0);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await expect(page.locator('[data-turn-id="streaming-math"] .katex'))
    .toHaveCount(1);
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("streaming formula closure preserves a non-bottom reading anchor", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?streaming-math=1");
  await wheelUntilTurn(
    page, "math-before-4", -1_200, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("close-streaming-formula").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(150);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a completed Mermaid diagram opens the shared pinch-zoom preview", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await applyProductionCsp(page);

  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect(preview).toBeVisible();
  const vector = preview.locator(".image-lightbox-vector > svg");
  await expect(vector).toBeVisible();
  await expect(preview.locator("img")).toHaveCount(0);
  const gesture = await pinchThenPanPreview(page);
  expect(gesture.afterPinch.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterPinch.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterPinch.x);
  await expect(preview).toBeVisible();
  await expect(preview).not.toHaveClass(/interacting/);
  await expect.poll(() => preview.locator(".image-lightbox-visual")
    .evaluate((node) => getComputedStyle(node).willChange)).toBe("auto");

  await page.getByRole("button", { name: "关闭 Mermaid 图表预览" }).click();
  await expect(preview).toHaveCount(0);

  await diagram.locator(".mermaid-svg").click();
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
  await page.evaluate(() => {
    document.querySelector<HTMLButtonElement>('[data-testid="switch-session"]')
      ?.click();
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("the real wide Robot Core diagram opens once and fits the viewport", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 1568, height: 870 });
  await page.goto("/tests/history-browser.html?actual-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  const trigger = diagram.locator(".mermaid-svg");
  await expect(trigger).toHaveJSProperty("tagName", "BUTTON");
  const inlineBounds = await trigger.evaluate((node) => {
    const svg = node.querySelector("svg");
    if (!svg) throw new Error("inline Mermaid SVG is missing");
    return {
      containerWidth: node.getBoundingClientRect().width,
      svgWidth: svg.getBoundingClientRect().width,
    };
  });
  expect(inlineBounds.svgWidth).toBeLessThanOrEqual(
    inlineBounds.containerWidth + 1,
  );

  await trigger.click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await expect.poll(async () => {
    if (pageErrors.length > 0) return `pageerror: ${pageErrors.join(" | ")}`;
    return await preview.isVisible() ? "visible" : "missing";
  }).toBe("visible");
  const bounds = await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stage: {
        left: stage.left,
        top: stage.top,
        right: stage.right,
        bottom: stage.bottom,
      },
      image: {
        left: image.left,
        top: image.top,
        right: image.right,
        bottom: image.bottom,
      },
    };
  });
  expect(bounds.image.left).toBeGreaterThanOrEqual(bounds.stage.left + 17);
  expect(bounds.image.right).toBeLessThanOrEqual(bounds.stage.right - 17);
  expect(bounds.image.top).toBeGreaterThanOrEqual(bounds.stage.top + 17);
  expect(bounds.image.bottom).toBeLessThanOrEqual(bounds.stage.bottom - 17);

  await preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const bounds = visual.getBoundingClientRect();
    node.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: (bounds.left + bounds.right) / 2,
      clientY: (bounds.top + bounds.bottom) / 2,
      detail: 1,
    }));
  });
  await expect(preview).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test("desktop trackpad wheel zooms around the pointer and pans the preview", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();

  const gesture = await wheelZoomThenPanPreview(page);
  expect(gesture.zoomPrevented).toBe(true);
  expect(gesture.panPrevented).toBe(true);
  expect(gesture.afterZoom.scale).toBeGreaterThan(1);
  expect(gesture.afterPan.scale).toBe(gesture.afterZoom.scale);
  expect(gesture.afterPan.x).toBeLessThan(gesture.afterZoom.x);
  expect(gesture.afterPan.y).toBeLessThan(gesture.afterZoom.y);
  await expect(page.getByRole("dialog", { name: "Mermaid 图表预览" }))
    .toBeVisible();
});

test("a wide zoomed Mermaid can pan to both horizontal edges", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop trackpad behavior");
  await page.setViewportSize({ width: 900, height: 720 });
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").nth(1);
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  await page.waitForTimeout(200);
  await preview.dispatchEvent("wheel", {
    ctrlKey: true,
    deltaY: -1_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);

  const horizontalEdges = async () => preview.evaluate((node) => {
    const visual = node.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    const stage = node.getBoundingClientRect();
    const image = visual.getBoundingClientRect();
    return {
      stageLeft: stage.left,
      stageRight: stage.right,
      imageLeft: image.left,
      imageRight: image.right,
    };
  });
  await preview.dispatchEvent("wheel", {
    deltaX: 5_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const rightEdge = await horizontalEdges();
  expect(Math.abs(rightEdge.imageRight - rightEdge.stageRight)).toBeLessThan(1);

  await preview.dispatchEvent("wheel", {
    deltaX: -10_000,
    clientX: 450,
    clientY: 360,
  });
  await page.waitForTimeout(120);
  const leftEdge = await horizontalEdges();
  expect(Math.abs(leftEdge.imageLeft - leftEdge.stageLeft)).toBeLessThan(1);
});

test("desktop Mermaid content clicks are inert and the backdrop closes", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "desktop mouse behavior");
  await page.goto("/tests/history-browser.html?mermaid=1");
  const diagram = page.locator(".mermaid-block").first();
  await expect(diagram).toHaveAttribute("data-mermaid-state", "ready");
  await diagram.locator(".mermaid-zoom").click();
  const preview = page.getByRole("dialog", { name: "Mermaid 图表预览" });
  const visual = preview.locator(".image-lightbox-visual");
  const scale = () => visual.evaluate((node) => {
    const matrix = new DOMMatrix(node.style.transform);
    return Math.hypot(matrix.a, matrix.b);
  });

  await preview.click({ position: { x: 450, y: 360 } });
  await expect(preview).toBeVisible();
  await expect.poll(scale).toBeCloseTo(1, 5);

  await preview.click({ position: { x: 5, y: 5 } });
  await expect(preview).toHaveCount(0);
});

test("invalid Mermaid falls back to copyable source", async ({ page }) => {
  await page.goto("/tests/history-browser.html?invalid-mermaid=1");
  const diagram = page.locator(".mermaid-block");
  await expect(diagram).toHaveAttribute("data-mermaid-state", "error");
  await expect(diagram.locator(".mermaid-source")).toContainText(
    "this is not a supported diagram",
  );
  await expect(diagram.getByRole("button", {
    name: "复制 Mermaid 源码",
  })).toBeVisible();
  await expect(diagram.locator(".mermaid-svg")).toHaveCount(0);
});

test("offscreen historical Mermaid does not load until its row is mounted", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?mermaid-history=1");
  await expect(page.locator('[data-turn-id="after-mermaid-40"]')).toBeVisible();
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  const before = await page.evaluate(() => performance.getEntriesByType("resource")
    .map((entry) => entry.name));
  expect(before.some((url) =>
    /\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i.test(url),
  )).toBe(false);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const diagramTurn = page.locator('[data-turn-id="mermaid"]');
  await expect(diagramTurn).toBeVisible();
  await expect(diagramTurn.locator(".mermaid-block")).toHaveCount(2);
  await expect(diagramTurn.locator(".mermaid-svg")).toHaveCount(2);
});

test("switching sessions discards an in-flight Mermaid render", async ({
  page,
}) => {
  await page.route(/\/node_modules\/\.vite\/deps\/mermaid(?:\.js|-)/i, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  await page.goto("/tests/history-browser.html?mermaid=1");
  await expect(page.locator(".mermaid-block")).toHaveCount(2);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(400);
  await expect(page.locator(".mermaid-block")).toHaveCount(0);
  await expect(page.locator(
    "body > svg, body > div[id*='cc-remote-mermaid']",
  )).toHaveCount(0);

  await page.getByTestId("switch-session").click();
  await expect(page.locator(
    '[data-turn-id="mermaid"] .mermaid-svg',
  )).toHaveCount(2);
});

test("a pending composer image previews without triggering removal", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?composer-attachment=1");
  const preview = page.getByRole("button", { name: "预览待发送图片 1" });
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await expect(page.locator(".image-lightbox img.image-lightbox-image"))
    .toBeVisible();
  await expect(page.locator(".image-lightbox-vector")).toHaveCount(0);
  await page.getByRole("button", { name: "关闭图片预览" }).click();
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await preview.click();
  await expect(page.locator(".image-lightbox")).toBeVisible();
  await page.locator(".image-lightbox").evaluate((node) => {
    const stage = node as HTMLElement;
    const visual = stage.querySelector<HTMLElement>(".image-lightbox-visual");
    if (!visual) throw new Error("lightbox visual is missing");
    Object.defineProperties(stage, {
      setPointerCapture: { configurable: true, value: () => {} },
      releasePointerCapture: { configurable: true, value: () => {} },
      hasPointerCapture: { configurable: true, value: () => false },
    });
    const bounds = visual.getBoundingClientRect();
    const x = (bounds.left + bounds.right) / 2;
    const y = (bounds.top + bounds.bottom) / 2;
    stage.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
      buttons: 1,
    }));
    stage.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true,
      cancelable: true,
      pointerId: 73,
      pointerType: "touch",
      clientX: x,
      clientY: y,
    }));
    stage.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      detail: 1,
    }));
  });
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
  await expect(preview).toBeVisible();

  await page.getByRole("button", { name: "移除待发送图片 1" }).click();
  await expect(preview).toHaveCount(0);
  await expect(page.locator(".image-lightbox")).toHaveCount(0);
});

test("a page waits through post-touch momentum and restores its final boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  // The network page may remain ready for multiple frames, but it cannot
  // rebase the still-active gesture onto a not-yet-mounted older row.
  await page.waitForTimeout(100);
  await dispatchTouchPhase(page, "touchend", 220);
  // touchend does not end iOS scroll ownership: native momentum continues to
  // emit scroll events without a finger. Keep the page staged, allow those
  // movements and unrelated row growth, then commit at the final idle point.
  await page.waitForTimeout(60);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  let momentumScrollTop = 0;
  for (const top of [240, 420, 540]) {
    momentumScrollTop = await viewport.evaluate((node, scrollTop) => {
      node.scrollTop = scrollTop;
      node.dispatchEvent(new Event("scroll"));
      return node.scrollTop;
    }, top);
    await page.waitForTimeout(50);
  }
  // Capture the final momentum boundary before any slow-runner command can
  // legitimately cross the 260 ms idle lease and mount the staged page.
  const momentumEnd = await readingAnchor(page);
  expect(momentumScrollTop).toBeGreaterThan(200);

  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect.poll(async () => (await readingAnchor(page)).id)
    .toBe(momentumEnd.id);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(momentumEnd.id);
  expect(Math.abs(settled.offset - momentumEnd.offset)).toBeLessThan(2);
});

test("a delayed pre-commit touchmove never rebases a retained page", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);

  // Create the reverse move before the page request. WebKit may queue that
  // native event and deliver it only after React commits the prepend.
  await stageDelayedTouchMove(page, 160, 80);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await dispatchDelayedTouchMove(page);
  await viewport.evaluate((node) => {
    node.scrollTop = 400;
    node.dispatchEvent(new Event("scroll"));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await dispatchTouchPhase(page, "touchend", 80);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("a cached-newer page that finishes under touch keeps its retained row", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 220);
  await dispatchTouchPhase(page, "touchmove", 160);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await dispatchTouchPhase(page, "touchend", 160);

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(350);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("movement while a page is staged becomes the release boundary", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The response is staged, while the same finger deliberately reverses
  // toward newer content before it is lifted. That real position becomes the
  // keyed anchor used when the page finally mounts.
  await dispatchTouchPhase(page, "touchmove", 80);
  await viewport.evaluate((node) => { node.scrollBy({ top: 720 }); });
  await expect.poll(async () => (await readingAnchor(page)).id)
    .not.toBe(original.id);
  // WebKit dispatches scroll before the virtualizer has necessarily committed
  // the newly visible row measurements. Freeze the user's actual settled
  // reading position, not that intermediate layout frame.
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  await dispatchTouchPhase(page, "touchend", 80);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("continuing to pull at the top keeps the staged page invisible", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit touch settlement");
  await page.goto("/tests/history-browser.html?delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const original = await readingAnchor(page);

  await dispatchTouchPhase(page, "touchstart", 160);
  await dispatchTouchPhase(page, "touchmove", 220);
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await page.waitForTimeout(50);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);

  // The same finger keeps pulling into the top edge. The old page cannot move
  // the DOM out from under that gesture; it mounts once, after release.
  await dispatchTouchPhase(page, "touchmove", 280);
  await viewport.evaluate((node) => { node.scrollBy({ top: -720 }); });
  await waitForScrollIdle(page);
  const moved = await readingAnchor(page);
  expect(moved.id).toBe(original.id);
  await dispatchTouchPhase(page, "touchend", 280);
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();

  await expect.poll(async () => (await readingAnchor(page)).id).toBe(moved.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - moved.offset),
  ).toBeLessThan(2);
  await page.waitForTimeout(300);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(moved.id);
  expect(Math.abs(settled.offset - moved.offset)).toBeLessThan(2);
});

test("repeated prepends preserve each page boundary instead of jumping to the inserted page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?pages=4&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");

  for (let pageNumber = 1; pageNumber <= 4; pageNumber += 1) {
    if (pageNumber === 1) {
      await viewport.evaluate((node) => { node.scrollTop = 0; });
    }
    await waitForScrollIdle(page);
    const before = await readingAnchor(page);
    const beforeScrollHeight = await viewport.evaluate((node) => node.scrollHeight);
    if (pageNumber === 1) {
      await requestOlderHistory(page, testInfo.project.name);
    } else {
      await page.locator(".load-more-btn").dispatchEvent("click");
    }
    await expect(page.getByTestId("load-count")).toHaveText(String(pageNumber));
    const insertedOldestId = pageNumber === 1 ? "n1" : `p${pageNumber}-1`;
    await expect.poll(async () =>
      viewport.evaluate((node) => node.scrollHeight),
    ).toBeGreaterThan(beforeScrollHeight);

    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    expect((await readingAnchor(page)).id).not.toBe(insertedOldestId);
    // End the wheel/touch gesture before pulling the next page. The product
    // intentionally allows only one request per physical gesture.
    await page.waitForTimeout(250);
  }
});

test("the first runtime-to-browse page preserves its captured reading row", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?runtime-browse=1&delay=350&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await expect(page.locator('[data-turn-id="n8"]')).toBeAttached();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);
});

test("one upward gesture starts at most one older-page request", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("load-count")).toHaveText("1");
});

test("cached-newer append with head eviction preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5&manual-growth=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await requestNewerHistory(page, testInfo.project.name);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
  await expect.poll(async () =>
    Math.abs((await readingAnchor(page)).offset - before.offset),
  ).toBeLessThan(2);

  // A late image/Markdown measurement before the retained row must reuse the
  // same transaction instead of introducing a second scroll writer.
  await page.getByTestId("grow-row").click();
  await expect(page.locator('[data-turn-id="m15"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const settled = await readingAnchor(page);
  expect(settled.id).toBe(before.id);
  expect(Math.abs(settled.offset - before.offset)).toBeLessThan(2);
});

test("one downward gesture starts at most one cached-newer page", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=80");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name, 2);
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");
});

test("repeated cached-newer pages keep the protected row through the final page", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=5");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);
  const expectedNewest = ["m28", "m36", "m40"];

  for (let index = 0; index < expectedNewest.length; index += 1) {
    const before = await readingAnchor(page);
    await page.getByRole("button", { name: "加载更新的历史" })
      .dispatchEvent("click");
    await expect(page.getByTestId("newer-load-count"))
      .toHaveText(String(index + 1));
    await expect(page.getByTestId("newest-turn-id"))
      .toHaveText(expectedNewest[index]);
    await expect.poll(async () => (await readingAnchor(page)).id).toBe(before.id);
    await expect.poll(async () =>
      Math.abs((await readingAnchor(page)).offset - before.offset),
    ).toBeLessThan(2);
    await page.waitForTimeout(80);
  }
  await expect(page.getByRole("button", {
    name: "加载更新的历史",
  })).toHaveCount(0);
});

test("browse live updates stay passive until the user returns to latest", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1");
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await wheelUntilTurn(page, "m12", -1_200, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);

  const returnButton = page.getByRole("button", { name: "回到最新" });
  await expect(returnButton).toHaveText("");
  const buttonBox = await returnButton.boundingBox();
  expect(buttonBox).not.toBeNull();
  expect(Math.abs((buttonBox?.width ?? 0) - (buttonBox?.height ?? 0)))
    .toBeLessThan(1);
  await returnButton.click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "回到最新" })).toHaveCount(0);
});

test("a delayed cached-newer page cannot move another session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?deep-browse=1&delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await requestNewerHistory(page, testInfo.project.name);
  await expect(page.getByTestId("history-page-activity")).toBeVisible();
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.waitForTimeout(500);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="m28"]')).toHaveCount(0);
});

test("an empty final page removes the loader without moving the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?empty-final=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const before = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect(page.getByRole("button", {
    name: "加载更早的历史",
  })).toHaveCount(0);
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("user movement after prepend stays stable through delayed growth", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?manual-growth=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  const initial = await readingAnchor(page);
  await requestOlderHistory(page, testInfo.project.name);
  await expect.poll(async () => (await readingAnchor(page)).id).toBe(initial.id);
  await wheelUntilTurn(page, "o2", 300, testInfo.project.name);
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  const sampled = await maxPaintedTurnOffsetShiftThroughAction(
    page,
    before.id,
    "grow-row",
  );
  await expect(page.locator('[data-turn-id="n8"] p')).toHaveCount(28);
  await waitForScrollIdle(page);
  const after = await readingAnchor(page);
  expect(sampled.missing).toBe(false);
  expect(sampled.maxShift).toBeLessThan(2);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("a delayed page from the previous session cannot move the new session", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=350");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="o4"]')).toBeVisible();
});

test("a generation change cancels the old page anchor and permits a new request", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=1000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  await page.locator(".load-more-btn").dispatchEvent("click");
  await expect(page.getByTestId("load-count")).toHaveText("2");
});

test("a generation change clears the automatic keyboard paging boundary", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "desktop keyboard path");
  await page.goto(
    "/tests/history-browser.html?generation-shift=1&delay=10000&manual-growth=1",
  );
  const viewport = page.locator(".thread");
  await viewport.focus();
  await viewport.press("Home");
  await expect(page.getByTestId("load-count")).toHaveText("1");
  await expect(page.getByTestId("history-page-activity")).toBeVisible();

  await page.getByTestId("shift-generation").click();
  await expect(page.getByTestId("history-page-activity")).toHaveCount(0);
  const keyboardBaseline = await viewport.evaluate((node) => {
    // Chromium may expose the End position before delivering its scroll event,
    // then coalesce that event with the following Home movement. Reproduce the
    // ordering deterministically: keydown sees the physical bottom position,
    // while the only delivered scroll event observes the final top position.
    node.scrollTop = node.scrollHeight;
    const bottom = node.scrollTop;
    node.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      key: "Home",
    }));
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
    return bottom;
  });
  expect(keyboardBaseline).toBeGreaterThan(0);
  await expect(page.getByTestId("load-count")).toHaveText("2");
});

test("same-session revision replacement resets to the latest row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator('[data-turn-id="m1"]')).toBeVisible();

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m1"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
  await expect(page.locator('[data-turn-id="r1"]')).toHaveCount(0);
});

test("pending history handoffs retain rows only within one authority", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?large=40&pending-revision-replace=1",
  );
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("pending");
  const samples = await page.evaluate(async () => {
    const readings: Array<{
      count: number;
      id: string | null;
      offset: number | null;
      scrollTop: number;
    }> = [];
    const deadline = performance.now() + 100;
    while (performance.now() < deadline) {
      await new Promise<void>((resolveFrame) =>
        requestAnimationFrame(() => resolveFrame()));
      const viewport = document.querySelector<HTMLElement>(".thread");
      if (!viewport) throw new Error("thread viewport is missing");
      const viewportRect = viewport.getBoundingClientRect();
      const rows = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")]
        .map((row) => ({ row, rect: row.getBoundingClientRect() }))
        .filter(({ rect }) =>
          rect.bottom > viewportRect.top && rect.top < viewportRect.bottom)
        .sort((left, right) =>
          Math.abs(left.rect.top - viewportRect.top)
          - Math.abs(right.rect.top - viewportRect.top));
      readings.push({
        count: document.querySelectorAll("[data-turn-id]").length,
        id: rows[0]?.row.dataset.turnId ?? null,
        offset: rows[0] ? rows[0].rect.top - viewportRect.top : null,
        scrollTop: viewport.scrollTop,
      });
    }
    return readings;
  });
  expect(samples.length).toBeGreaterThan(2);
  expect(samples.every((sample) => sample.count > 0)).toBe(true);
  expect(samples.every((sample) => sample.id === before.id)).toBe(true);
  expect(samples.every((sample) => sample.offset != null
    && Math.abs(sample.offset - before.offset) < 2)).toBe(true);
  expect(samples.every((sample) => sample.scrollTop > 1)).toBe(true);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="m10"]')).toHaveCount(0);
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();

  await page.getByTestId("replace-authority").click();
  await expect(page.getByTestId("session-authority-scope"))
    .toHaveText("fixture-authority-b");
  expect(await page.locator('[data-turn-id="r24"]').count()).toBe(0);

  await expect(page.getByTestId("history-transition-state"))
    .toHaveText("ready");
  await expect(page.locator('[data-turn-id="r24"]')).toBeVisible();
});

test("replay recovery replacement preserves the current reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&recovery-replace=1");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await wheelUntilTurn(page, "m10", -400, testInfo.project.name);
  await waitForReadingPositionIdle(page);
  const before = await readingAnchor(page);

  await page.getByTestId("replace-revision").click();
  await expect(page.locator('[data-turn-id="m10"] p')).toHaveCount(4);
  await waitForReadingPositionIdle(page);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("reversing direction while a page is pending preserves the reading row", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?delay=700");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = 0; });
  await requestOlderHistory(page, testInfo.project.name);
  await wheelUntilTurn(page, "o4", 2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await expect(page.locator('[data-turn-id="n8"]')).toHaveCount(1);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("virtualization bounds mounted rows and preserves an expanded timeline", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".turn-process-head")).toHaveAttribute("aria-expanded", "true");
});

test("plan progress uses a compact popover that closes outside", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  const trigger = timeline.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await trigger.click();

  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("验证计划弹层");

  const box = await popover.boundingBox();
  const viewport = page.viewportSize();
  const anchorBox = await trigger.boundingBox();
  if (!box || !viewport || !anchorBox) {
    throw new Error("plan popover has no geometry");
  }
  expect(box.x).toBeGreaterThanOrEqual(8);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 8);
  expect(box.y).toBeGreaterThanOrEqual(8);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 8);
  const expectedCenter = Math.min(
    Math.max(anchorBox.x + anchorBox.width / 2, 16 + box.width / 2),
    viewport.width - 16 - box.width / 2,
  );
  expect(Math.abs(box.x + box.width / 2 - expectedCenter)).toBeLessThan(2);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);

  await trigger.click();
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(popover).toHaveCount(0);
  await trigger.click();
  await expect(popover).toBeVisible();

  await page.getByTestId("load-count").click();
  await expect(popover).toHaveCount(0);
});

test("historical Plan flips below a trigger near the top edge", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const trigger = page.locator('[data-turn-id="timeline"]')
    .getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  if (isMobileWebKitProject(testInfo.project.name)) {
    await dispatchTouchGesture(page, -60);
  } else {
    await page.locator(".thread").dispatchEvent("wheel", { deltaY: 80 });
  }
  await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) throw new Error("historical Plan has no thread viewport");
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    viewport.scrollTop += triggerBox.y - (viewportBox.y + 20);
  });
  await expect.poll(() => trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    if (!viewport) return false;
    const viewportBox = viewport.getBoundingClientRect();
    const triggerBox = node.getBoundingClientRect();
    const relativeTop = triggerBox.y - viewportBox.y;
    const settled = relativeTop >= 16 && relativeTop < 24;
    if (!settled) {
      viewport.scrollTop += relativeTop - 20;
    }
    return settled && triggerBox.bottom <= viewportBox.bottom;
  })).toBe(true);

  await trigger.click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toBeVisible();
  await expect(popover).toHaveAttribute("data-placement", "below");
  const { threadBox, anchorBox, popoverBox } = await trigger.evaluate((node) => {
    const viewport = node.closest<HTMLElement>(".thread");
    const dialog = document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="计划进度"]',
    );
    if (!viewport || !dialog) throw new Error("near-top Plan has no geometry");
    const bounds = (element: Element) => {
      const box = element.getBoundingClientRect();
      return {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
      };
    };
    return {
      threadBox: bounds(viewport),
      anchorBox: bounds(node),
      popoverBox: bounds(dialog),
    };
  });
  expect(Math.abs(popoverBox.y - (anchorBox.y + anchorBox.height + 8)))
    .toBeLessThan(2);
  expect(popoverBox.height).toBeGreaterThan(64);
  expect(popoverBox.y + popoverBox.height)
    .toBeLessThanOrEqual(threadBox.y + threadBox.height - 15);
});

test("a long active turn keeps its plan beside the composer", async ({ page }) => {
  await page.goto("/tests/history-browser.html?persistent-plan=1");
  const thread = page.locator(".thread");
  await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(() => thread.evaluate((node) => node.scrollTop))
    .toBeGreaterThan(200);
  await expect(page.locator('[data-turn-id="persistent-plan"]')
    .getByRole("button", { name: /查看计划进度/ })).toHaveCount(0);

  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("an old terminal plan stays with its historical turn", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?historical-plan=1");

  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const planTurn = page.locator('[data-turn-id="historical-plan"]');
  await expect(planTurn).toBeVisible();
  const trigger = planTurn.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(1);

  await trigger.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("验证计划弹层");
});

test("iOS pointercancel releases the plan trigger without opening it", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const trigger = page.getByRole("button", { name: /查看计划进度/ });
  await expect(trigger).toBeVisible();

  await dispatchCancelledTouchTap(trigger, 183);

  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toHaveCount(0);
});

test("terminal turn does not mark unfinished structured plan complete", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=terminal");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("1 / 3");
  await expect(popover).toContainText("本轮已结束，计划未更新");
  await expect(popover).not.toContainText("全部完成");
});

test("unstructured plan detail remains visible in the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=unstructured");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(popover).toContainText("已记录");
  await expect(popover).not.toContainText("全部完成");
  await expect(popover).toContainText("先检查协议，再验证移动端，最后发布。");
});

test("opening a cached plan refreshes authoritative detail", async ({ page }) => {
  await page.goto("/tests/history-browser.html?plan-ui=refresh");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const popover = page.getByRole("dialog", { name: "计划进度" });
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("loading");
  await expect(popover).toBeVisible();
  await expect(popover).toContainText("缓存步骤二");
  await expect(page.getByTestId("plan-refresh-state")).toHaveText("ready");
  await expect(popover).toContainText("权威步骤二");
  await expect(popover).not.toContainText("缓存步骤二");
});

test("only the selected plan block moves into the compact popover", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-ui=mixed");
  await page.locator(".turn-process-head").click();
  await expect(page.getByText("旧版计划", { exact: true })).toBeVisible();
});

test("a turn plan without a Goal stays in a compact session-level strip", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip).toContainText("实现固定入口");
  const box = await chip.boundingBox();
  if (!box) throw new Error("plan strip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);

  await chip.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("1 / 3");
  await expect(dialog).toContainText("完成浏览器回归");
  await expect(page.locator(".scrim.show")).toHaveCount(0);
  await expect(page.locator(".plan-sheet")).toHaveCount(0);
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox) throw new Error("plan popover has no geometry");
  expect(dialogBox.width).toBeLessThan(361);
  const anchorBox = await chip.boundingBox();
  if (!anchorBox) throw new Error("plan popover has no anchor");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2
      - (anchorBox.x + anchorBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height - (anchorBox.y - 8),
  )).toBeLessThan(2);
  const openChipBox = await chip.boundingBox();
  if (!openChipBox) throw new Error("open plan strip has no geometry");
  expect(Math.abs(openChipBox.x - box.x)).toBeLessThan(1);
  expect(Math.abs(openChipBox.y - box.y)).toBeLessThan(1);

  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("standalone Plan closes from outside and fits the mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 720 });
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("mobile plan has no geometry");
  const anchor = page.getByRole("button", { name: /查看计划进度/ });
  const anchorBox = await anchor.boundingBox();
  if (!anchorBox) throw new Error("mobile plan has no anchor");
  expect(box.x).toBeGreaterThanOrEqual(11);
  expect(box.x + box.width).toBeLessThanOrEqual(379);
  const visualBottom = await page.evaluate(() => (
    (window.visualViewport?.offsetTop ?? 0)
      + (window.visualViewport?.height ?? window.innerHeight)
  ));
  expect(box.y + box.height).toBeLessThanOrEqual(visualBottom - 11);
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  await page.locator("[data-testid=goal-fixture-content]").click({
    position: { x: 5, y: 5 },
  });
  await expect(dialog).toHaveCount(0);
});

test("Plan follows chat and composer geometry changes", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1");
  await page.getByRole("button", { name: /查看计划进度/ }).click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  const initialBox = await dialog.boundingBox();
  const initialAnchorBox = await chip.boundingBox();
  if (!initialBox || !initialAnchorBox) {
    throw new Error("Plan has no initial geometry");
  }
  await page.getByTestId("goal-fixture-composer").evaluate((node) => {
    node.style.height = "176px";
  });
  await expect.poll(async () => {
    const anchorBox = await chip.boundingBox();
    return anchorBox?.y ?? null;
  }).toBeLessThan(initialAnchorBox.y - 40);
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.y + currentDialog.height - (currentAnchor.y - 8),
    );
  }).toBeLessThan(2);
  const [resizedDialogBox, resizedAnchorBox, composerBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
    page.getByTestId("goal-fixture-composer").boundingBox(),
  ]);
  if (!resizedDialogBox || !resizedAnchorBox || !composerBox) {
    throw new Error("tall-composer Plan fixture has no geometry");
  }
  expect(Math.abs(
    resizedDialogBox.y + resizedDialogBox.height - (resizedAnchorBox.y - 8),
  )).toBeLessThan(2);
  expect(resizedDialogBox.y + resizedDialogBox.height).toBeLessThanOrEqual(
    composerBox.y - 15,
  );

  await page.getByTestId("goal-fixture-spacer").evaluate((node) => {
    node.style.width = "180px";
  });
  await expect.poll(async () => {
    const [currentDialog, currentAnchor] = await Promise.all([
      dialog.boundingBox(),
      chip.boundingBox(),
    ]);
    if (!currentDialog || !currentAnchor) return null;
    return Math.abs(
      currentDialog.x + currentDialog.width / 2
        - (currentAnchor.x + currentAnchor.width / 2),
    );
  }).toBeLessThan(2);
});

test("a long Plan scrolls within the space above its anchor", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 520 });
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=none&plan=1&plan-long=1",
  );
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await chip.click();
  const dialog = page.getByRole("dialog", { name: "计划进度" });
  await expect(dialog).toBeVisible();
  const [box, anchorBox] = await Promise.all([
    dialog.boundingBox(),
    chip.boundingBox(),
  ]);
  if (!box || !anchorBox) throw new Error("long Plan has no geometry");
  expect(Math.abs(box.y + box.height - (anchorBox.y - 8)))
    .toBeLessThan(2);
  expect(box.y).toBeGreaterThanOrEqual(15);
  expect(await dialog.evaluate((node) => ({
    clientHeight: node.clientHeight,
    scrollHeight: node.scrollHeight,
    overflowY: getComputedStyle(node).overflowY,
  }))).toMatchObject({ overflowY: "auto" });
  expect(await dialog.evaluate((node) => node.scrollHeight))
    .toBeGreaterThan(await dialog.evaluate((node) => node.clientHeight));
});

test("a completed session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=1");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.complete")).toHaveCount(1);
  await expect(chip).toContainText("2 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an interrupted session Plan disappears when the next message begins", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?plan-lifecycle=interrupted");
  const chip = page.getByRole("button", { name: /查看计划进度/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator(".plan-chip-ring.failed")).toHaveCount(1);
  await expect(chip).toContainText("1 / 2");

  await page.getByTestId("send-next-plan-message").click();
  await expect(chip).toHaveCount(0);
});

test("an existing Goal owns the turn plan in its detail sheet", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1");
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);
  await page.getByRole("button", { name: /查看 Goal/ }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toBeVisible();
  await expect(planEntry).toContainText("实现固定入口");
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("0");
  await planEntry.click();
  await expect(page.getByTestId("plan-detail-requests")).toHaveText("1");
  await expect(dialog.getByLabel("计划执行状态")).toContainText("1 / 3");
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a completed Goal keeps the Plan from its own final turn", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toHaveCount(0);

  await page.getByRole("button", { name: /查看 Goal/ }).click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" })
    .getByRole("button", { name: /查看计划进度/ })).toBeVisible();
});

test("a new turn retires its completed Goal and owns a standalone Plan", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?goal-ui=1&goal-status=complete&plan=1&goal-next-turn=1",
  );
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);

  const plan = page.getByRole("button", { name: /查看计划进度/ });
  await expect(plan).toBeVisible();
  await expect(plan).toContainText("实现固定入口");
  await plan.click();
  await expect(page.getByRole("dialog", { name: "计划进度" }))
    .toContainText("完成浏览器回归");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("a long Goal keeps its merged plan in the detail sheet first viewport", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&plan=1&goal-long=1");
  await page.getByRole("button", { name: /查看 Goal/ }).click();

  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  const planEntry = dialog.getByRole("button", { name: /查看计划进度/ });
  await expect(planEntry).toContainText("实现固定入口");
  await expect(planEntry).toBeInViewport();
  await expect(dialog.locator(".goal-sheet-scroll"))
    .toHaveJSProperty("scrollTop", 0);
  await planEntry.click();
  await expect(dialog.getByLabel("计划执行状态"))
    .toContainText("实现固定入口");
});

test("a hidden Goal cannot make the current plan inaccessible", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-hidden=1&plan=1");
  await expect(page.getByRole("button", { name: /查看 Goal/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /查看计划进度/ }))
    .toBeVisible();
});

test("goal entry stays compact and opens its editor", async ({ page }) => {
  await page.goto("/tests/history-browser.html?goal-ui=1");
  const chip = page.getByRole("button", { name: "查看 Goal" });
  await expect(chip).toBeVisible();
  const box = await chip.boundingBox();
  const viewport = page.viewportSize();
  if (!box || !viewport) throw new Error("goal chip has no geometry");
  expect(box.height).toBeLessThanOrEqual(42);
  expect(box.width).toBeLessThan(viewport.width - 20);

  await chip.click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();
  const dialogBox = await dialog.boundingBox();
  if (!dialogBox || !viewport) throw new Error("goal dialog has no geometry");
  expect(dialogBox.width).toBeLessThanOrEqual(Math.min(580, viewport.width));
  const chatBox = await page.locator(".thread-shell").boundingBox();
  if (!chatBox) throw new Error("goal dialog has no chat viewport");
  expect(Math.abs(
    dialogBox.x + dialogBox.width / 2 - (chatBox.x + chatBox.width / 2),
  )).toBeLessThan(2);
  expect(Math.abs(
    dialogBox.y + dialogBox.height / 2 - (chatBox.y + chatBox.height / 2),
  )).toBeLessThan(2);
  const statCards = dialog.locator(".goal-stats > div");
  await expect(statCards).toHaveCount(3);
  expect(await statCards.first().evaluate((node) =>
    getComputedStyle(node).borderTopWidth)).toBe("0px");
  const icon = dialog.locator(".goal-sheet-icon");
  expect(await icon.evaluate((node) => {
    const style = getComputedStyle(node);
    return style.backgroundColor !== style.color;
  })).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toHaveCount(0);
  await chip.click();
  await expect(page.getByRole("dialog", { name: "Codex Goal" }))
    .toBeVisible();
  await page.locator(".scrim.show").click({ position: { x: 5, y: 5 } });
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("remembered goal shows a compact recovery state without opening its editor", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=loading");
  const recovery = page.getByRole("status", { name: "正在恢复 Goal" });
  await expect(recovery).toBeVisible();
  await expect(recovery).toContainText("正在恢复…");
  await expect(page.getByRole("dialog", { name: "Codex Goal" })).toHaveCount(0);
});

test("budgeted goal keeps its blocked status visible on mobile", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?goal-ui=1&goal-status=blocked");
  const chip = page.getByRole("button", { name: /查看 Goal/ });
  await expect(chip).toHaveAttribute("aria-label", /受阻/);
  await expect(chip.locator(".goal-chip-ring"))
    .toHaveClass(/goal-chip-ring-blocked/);
});

test("goal editor stays inside the tablet visual viewport above the keyboard", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/tests/history-browser.html?goal-ui=1");
  await page.getByRole("button", { name: "查看 Goal" }).click();
  const dialog = page.getByRole("dialog", { name: "Codex Goal" });
  await expect(dialog).toBeVisible();

  const visualTop = 20;
  const visualHeight = 560;
  await page.evaluate(({ top, height }) => {
    const root = document.documentElement;
    root.style.setProperty("--app-offset-top", `${top}px`);
    root.style.setProperty("--app-height", `${height}px`);
    root.style.setProperty(
      "--keyboard-inset", `${window.innerHeight - top - height}px`,
    );
    window.dispatchEvent(new Event("resize"));
  }, { top: visualTop, height: visualHeight });
  await dialog.locator("textarea").focus();

  await expect.poll(async () => dialog.boundingBox()).not.toBeNull();
  const box = await dialog.boundingBox();
  if (!box) throw new Error("goal dialog has no tablet geometry");
  expect(box.y).toBeGreaterThanOrEqual(visualTop - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(
    visualTop + visualHeight + 1,
  );
  expect(Math.abs(
    box.y + box.height / 2 - (visualTop + visualHeight / 2),
  )).toBeLessThan(2);
});

test("desktop text selection keeps its original virtual turn while edge-dragging", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const startText = page.locator(
    `[data-turn-id="${startTurnId}"] p`,
  ).first();
  await expect(startText).toBeInViewport();
  const viewportBox = await viewport.boundingBox();
  const startPoint = await textSelectionPoint(startText);
  if (!viewportBox) {
    throw new Error("selection fixture has no geometry");
  }
  const startScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.move(startPoint.x, startPoint.y);
  await page.mouse.down();
  await page.mouse.move(
    viewportBox.x + viewportBox.width - 48,
    viewportBox.y + viewportBox.height - 2,
    { steps: 20 },
  );
  for (let step = 0; step < 12; step += 1) {
    await page.mouse.wheel(0, 220);
    await page.mouse.move(
      viewportBox.x + viewportBox.width - 48 + (step % 2),
      viewportBox.y + viewportBox.height - 2,
    );
    await page.waitForTimeout(45);
  }

  const draggedScrollTop = await viewport.evaluate((node) => node.scrollTop);
  const draggingSelection = await nativeSelectionSnapshot(page);
  expect(draggedScrollTop - startScrollTop).toBeGreaterThan(800);
  expect(draggingSelection.anchorTurnId).toBe(startTurnId);
  expect(draggingSelection.anchorConnected).toBe(true);
  expect(draggingSelection.text).toContain(startTurnId);
  await expect(page.locator(
    `[data-turn-id="${startTurnId}"]`,
  )).toBeAttached();
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "false",
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  await page.evaluate(() => new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => resolve());
  }));
  const immediateReleasedScrollTop =
    await viewport.evaluate((node) => node.scrollTop);
  expect(immediateReleasedScrollTop - startScrollTop).toBeGreaterThan(800);
  const nativeReleaseAdvance = immediateReleasedScrollTop - draggedScrollTop;
  expect(nativeReleaseAdvance).toBeGreaterThanOrEqual(-2);
  expect(nativeReleaseAdvance).toBeLessThan(viewportBox.height / 2);
  const immediateReleasedAnchor = await readingAnchor(page);
  // Chromium may finish one native selection auto-scroll step on mouseup and
  // cross a virtual-row boundary. The post-release position is authoritative;
  // the app must not replay an older scroll command after control returns.
  await page.waitForTimeout(120);
  const releasedAnchor = await readingAnchor(page);
  expect(releasedAnchor.id).toBe(immediateReleasedAnchor.id);
  expect(Math.abs(
    releasedAnchor.offset - immediateReleasedAnchor.offset,
  )).toBeLessThan(2);
  await page.locator(`[data-turn-id="${startTurnId}"]`).evaluate((node) => {
    (node as HTMLElement).style.paddingBottom = "320px";
  });
  await page.waitForTimeout(120);
  const measuredAnchor = await readingAnchor(page);
  expect(measuredAnchor.id).toBe(releasedAnchor.id);
  expect(Math.abs(measuredAnchor.offset - releasedAnchor.offset)).toBeLessThan(2);
  const measuredScrollTop = await viewport.evaluate((node) => node.scrollTop);
  await page.getByTestId("append-turn").evaluate(
    (button: HTMLButtonElement) => button.click(),
  );
  await page.waitForTimeout(100);
  expect(Math.abs(
    await viewport.evaluate((node) => node.scrollTop) - measuredScrollTop,
  )).toBeLessThan(2);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", {
      bubbles: true,
      cancelable: true,
    }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("desktop wheel scrolling remains available after text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=120");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startTurnId = (await readingAnchor(page)).id;
  const text = page.locator(`[data-turn-id="${startTurnId}"] p`).first();
  const box = await text.boundingBox();
  if (!box) throw new Error("selection fixture has no geometry");

  await page.mouse.move(box.x + 4, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(box.x + box.width - 4, box.x + 180),
    box.y + box.height / 2,
    { steps: 12 },
  );
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
  expect((await nativeSelectionSnapshot(page)).text.length).toBeGreaterThan(0);
  const beforeScrollTop = await viewport.evaluate((node) => node.scrollTop);

  await page.mouse.wheel(0, 640);
  await expect.poll(
    () => viewport.evaluate((node) => node.scrollTop),
  ).toBeGreaterThan(beforeScrollTop + 200);
  const afterScroll = await nativeSelectionSnapshot(page);
  expect(afterScroll.anchorConnected).toBe(true);
  expect(afterScroll.text.length).toBeGreaterThan(0);
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );
});

test("a late cached-newer page cannot evict an active text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&delay=3000",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await page.getByRole("button", { name: "加载更新的历史" })
    .dispatchEvent("click");
  await expect(page.getByTestId("newer-load-count")).toHaveText("1");

  await wheelUntilTurn(page, "m5", -400, testInfo.project.name);
  const startText = page.locator('[data-turn-id="m5"] p').first();
  await expect(startText).toBeInViewport();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection page fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 140),
    start.y,
    { steps: 8 },
  );
  await expect(viewport).toHaveAttribute(
    "data-text-selection-dragging", "true",
  );

  await expect(page.getByTestId("newest-turn-id")).toHaveText("m28");
  await expect(page.locator('[data-turn-id="m5"]')).toBeAttached();
  expect((await nativeSelectionSnapshot(page)).anchorTurnId).toBe("m5");
  await page.mouse.up();
  await page.evaluate(() => {
    document.dispatchEvent(new ClipboardEvent("copy", { bubbles: true }));
  });
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("switching sessions clears retained desktop text selection", async ({
  page,
}, testInfo) => {
  test.skip(isMobileWebKitProject(testInfo.project.name),
    "the configured WebKit project is a touch phone; this is a desktop mouse path");
  await page.goto("/tests/history-browser.html?large=80");
  const viewport = page.locator(".thread");
  await wheelUntilTurn(page, "m42", -600, testInfo.project.name);
  await waitForScrollIdle(page);
  const startText = page.locator('[data-turn-id="m42"] p').first();
  const start = await textSelectionPoint(startText);
  const textBox = await startText.boundingBox();
  if (!textBox) throw new Error("selection switch fixture has no geometry");
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(
    Math.min(textBox.x + textBox.width - 2, start.x + 120),
    start.y,
    { steps: 8 },
  );
  await page.mouse.up();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "true",
  );

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
  expect((await nativeSelectionSnapshot(page)).text).toBe("");
  expect(await page.locator(".turn").count()).toBeLessThan(40);
});

test("nested process disclosures survive virtual row unmounts", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?timeline=1&engine=claude");
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  const timeline = page.locator('[data-turn-id="timeline"]');
  await expect(timeline).toBeVisible();
  await timeline.locator(".turn-process-head").click();
  const activity = timeline.locator("details.process-activity");
  const reasoning = timeline.locator("details.process-reasoning");
  await activity.locator(":scope > summary").click();
  await reasoning.locator(":scope > summary").click();
  await expect(activity).toHaveAttribute("open", "");
  await expect(reasoning).toHaveAttribute("open", "");

  await scrollThreadToEdge(page, "end", testInfo.project.name);
  await expect(timeline).toHaveCount(0);
  await scrollThreadToEdge(page, "start", testInfo.project.name);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator("details.process-activity"))
    .toHaveAttribute("open", "");
  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("one stationary press opens a process timeline while a newer turn grows", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });

  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  await expect(header).toBeVisible();
  await expect(header).toHaveAttribute("aria-expanded", "false");
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");
  const point = {
    x: box.x + box.width / 2,
    y: box.y + box.height / 2,
  };

  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(header).toHaveAttribute("aria-expanded", "true");
  await expect(viewport).toHaveAttribute(
    "data-text-selection-retained", "false",
  );
});

test("one stationary press opens nested thinking while a newer turn grows", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  await waitForScrollIdle(page);
  const summary = timeline.locator(".process-reasoning > summary");
  const box = await summary.boundingBox();
  if (!box) throw new Error("nested reasoning summary has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").evaluate(
      (button: HTMLButtonElement) => button.click(),
    );
    await page.waitForTimeout(35);
  }
  await page.mouse.up();

  await expect(timeline.locator("details.process-reasoning"))
    .toHaveAttribute("open", "");
});

test("dragging a process header outside cannot leave output following locked", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?interactive-timeline=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const header = page.locator(
    '[data-turn-id="timeline"] .turn-process-head',
  );
  const box = await header.boundingBox();
  if (!box) throw new Error("process header has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(header).toHaveAttribute("aria-expanded", "false");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("dragging nested process thinking outside cannot leave output following locked", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  await timeline.locator(".turn-process-head").click();
  await waitForScrollIdle(page);
  const summary = timeline.locator(".process-reasoning > summary");
  const box = await summary.boundingBox();
  if (!box) throw new Error("nested reasoning summary has no bounds");

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width + 80, box.y + box.height + 80);
  await page.mouse.up();
  await expect(timeline.locator("details.process-reasoning"))
    .not.toHaveAttribute("open", "");

  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("iOS pointercancel releases process interactions and output following", async ({
  page,
}, testInfo) => {
  test.skip(!isMobileWebKitProject(testInfo.project.name),
    "iOS WebKit pointer cancellation");
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  const timeline = page.locator('[data-turn-id="timeline"]');
  const header = timeline.locator(".turn-process-head");

  await dispatchCancelledTouchTap(header, 184);
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await header.evaluate((node) => {
    const target = node as HTMLElement;
    target.dispatchEvent(new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      detail: 1,
    }));
  });
  await expect(header).toHaveAttribute("aria-expanded", "true");
  const reasoning = timeline.locator("details.process-reasoning");
  await dispatchCancelledTouchTap(
    reasoning.locator(":scope > summary"), 185,
  );
  await expect(reasoning).not.toHaveAttribute("open", "");
  await page.getByTestId("grow-stream").click();
  await expect.poll(() => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
});

test("live append follows at the bottom but not while reading history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-41"]')).toBeVisible();

  // Use the browser's native scroll pipeline here so the virtualizer and
  // React receive the same wheel/scroll ordering as a real user gesture.
  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  await expect(page.locator(".scroll-bottom-btn")).toBeVisible();
  await page.waitForTimeout(250);
  await expect(page.locator('[data-turn-id="live-41"]')).toHaveCount(0);
  const before = await readingAnchor(page);
  await page.getByTestId("append-turn").click();
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
  await expect(page.locator('[data-turn-id="live-42"]')).toHaveCount(0);
  expect(await page.locator(".turn").count()).toBeLessThan(40);
  await assertCodexBurstNeverPaintsAboveTail(page, testInfo.project.name);
});

test("scrolling a live-dirty history window to its latest edge restores the active tail", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?deep-browse=1&dirty-live-browse=1&engine=claude",
  );
  await expect(page.locator('[data-turn-id="m20"]')).toBeVisible();
  await page.getByTestId("append-turn").click();
  await expect(page.locator('[data-turn-id="live-streaming"]')).toHaveCount(0);

  for (let pageIndex = 0; pageIndex < 5; pageIndex += 1) {
    await scrollThreadToEdge(page, "end", testInfo.project.name);
    if (await page.locator('[data-turn-id="live-streaming"] .turn-working')
      .count()) break;
    await page.waitForTimeout(80);
  }

  await expect(page.locator('[data-turn-id="live-streaming"] .turn-working'))
    .toBeVisible();
  await expect(page.getByTestId("newest-turn-id")).toHaveText("live-streaming");
  await expect(page.locator(".scroll-bottom-btn")).toHaveCount(0);
});

test("returning to a background-grown live turn settles at its current tail", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?interactive-timeline=1&engine=claude",
  );
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect(page.locator('[data-turn-id="streaming"] .turn-working'))
    .toBeVisible();

  await page.getByTestId("switch-session").click();
  await expect(page.locator('[data-turn-id="b4"]')).toBeVisible();
  // Visibility can precede ChatView's next-frame session-entry tail settle on
  // a loaded WebKit worker. Measure the background update only after that
  // intentional scope transition has finished.
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.getByTestId("grow-background-stream").click();
  await page.waitForTimeout(200);
  const unchanged = await readingAnchor(page);
  expect(unchanged.id).toBe(before.id);
  expect(Math.abs(unchanged.offset - before.offset)).toBeLessThan(2);

  await page.getByTestId("switch-session").click();
  const working = page.locator(
    '[data-turn-id="streaming"] .turn-working',
  );
  await expect(working).toBeVisible();
  await expect.poll(async () => working.evaluate((node) => {
    const thread = document.querySelector<HTMLElement>(".thread");
    if (!thread) return false;
    return node.getBoundingClientRect().bottom
      <= thread.getBoundingClientRect().bottom + 1;
  })).toBe(true);
  for (let index = 0; index < 4; index += 1) {
    await page.getByTestId("grow-stream").click();
    await expect.poll(async () => working.evaluate((node) => {
      const thread = document.querySelector<HTMLElement>(".thread");
      if (!thread) return false;
      return node.getBoundingClientRect().bottom
        <= thread.getBoundingClientRect().bottom + 1;
    })).toBe(true);
  }
});

async function assertCodexBurstNeverPaintsAboveTail(
  page: import("@playwright/test").Page,
  projectName: string,
): Promise<void> {
  await page.goto("/tests/history-browser.html?codex-live-burst=1");
  const viewport = page.locator(".thread");
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await waitForScrollIdle(page);

  const resultPromise = page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !start) throw new Error("Codex burst fixture is incomplete");
    const distances: number[] = [];
    const violations: Array<{
      frame: number;
      distance: number;
      scrollTop: number;
      scrollHeight: number;
      burst: string | undefined;
    }> = [];
    const reverseJumps: Array<{
      frame: number;
      previousScrollTop: number;
      scrollTop: number;
      previousScrollHeight: number;
      scrollHeight: number;
    }> = [];
    let frames = 0;
    let doneFrames = 0;
    let previousScrollTop = thread.scrollTop;
    let previousScrollHeight = thread.scrollHeight;
    let previousBurst = document.documentElement.dataset.codexBurst;
    start.click();
    await new Promise<void>((resolve) => {
      const sample = () => {
        frames += 1;
        const distance = Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        );
        distances.push(distance);
        if (distance > 2) {
          violations.push({
            frame: frames,
            distance,
            scrollTop: thread.scrollTop,
            scrollHeight: thread.scrollHeight,
            burst: document.documentElement.dataset.codexBurst,
          });
        }
        const burst = document.documentElement.dataset.codexBurst;
        if (burst === "running" && previousBurst === "running"
            && thread.scrollTop < previousScrollTop - 2) {
          reverseJumps.push({
            frame: frames,
            previousScrollTop,
            scrollTop: thread.scrollTop,
            previousScrollHeight,
            scrollHeight: thread.scrollHeight,
          });
        }
        previousScrollTop = thread.scrollTop;
        previousScrollHeight = thread.scrollHeight;
        previousBurst = burst;
        if (burst === "done") {
          doneFrames += 1;
          if (doneFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(() => window.setTimeout(sample, 0));
      };
      requestAnimationFrame(() => window.setTimeout(sample, 0));
    });
    return {
      frames,
      worst: Math.max(0, ...distances),
      reverseJumps,
      violations,
    };
  });

  const result = await resultPromise;
  expect(result.frames).toBeGreaterThan(10);
  expect(result.violations, JSON.stringify(result.violations)).toHaveLength(0);
  expect(result.reverseJumps, JSON.stringify(result.reverseJumps))
    .toHaveLength(0);
  expect(result.worst).toBeLessThanOrEqual(2);

  // The pre-paint observer is active only while the newest turn is open. A
  // reader who has deliberately left the tail must retain the exact row while
  // the same long Codex tool burst continues in the background.
  await page.goto("/tests/history-browser.html?codex-live-burst=1");
  await wheelUntilTurn(page, "burst-history-1", -2_000, projectName);
  const before = await readingAnchor(page);
  await page.getByTestId("start-codex-burst").click();
  await expect.poll(() => page.evaluate(
    () => document.documentElement.dataset.codexBurst,
  )).toBe("done");
  await page.waitForTimeout(100);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
}

test("multi-line IME growth stays pinned during a Codex tool burst", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  const result = await page.evaluate(async () => {
    const thread = document.querySelector<HTMLElement>(".thread");
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-testid="live-composer-shell"] textarea',
    );
    const start = document.querySelector<HTMLButtonElement>(
      '[data-testid="start-codex-burst"]',
    );
    if (!thread || !input || !start) {
      throw new Error("live composer fixture is incomplete");
    }
    const frame = () => new Promise<void>((resolve) => {
      requestAnimationFrame(() => resolve());
    });
    thread.scrollTop = thread.scrollHeight;
    await frame();
    await frame();

    const distances: number[] = [];
    let typed = false;
    let terminalFrames = 0;
    const monitor = new Promise<void>((resolve) => {
      const sample = () => {
        distances.push(Math.max(
          0,
          thread.scrollHeight - thread.scrollTop - thread.clientHeight,
        ));
        if (typed && document.documentElement.dataset.codexBurst === "done") {
          terminalFrames += 1;
          if (terminalFrames >= 6) {
            resolve();
            return;
          }
        }
        requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
    });

    const setValue = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype, "value",
    )?.set;
    if (!setValue) throw new Error("textarea value setter is unavailable");
    const values = Array.from(
      { length: 5 },
      (_, index) => Array.from(
        { length: index + 1 },
        (__, line) => `第 ${line + 1} 行拼音输入内容用于验证输入框稳定`,
      ).join("\n"),
    );
    const heights: number[] = [input.getBoundingClientRect().height];
    input.focus();
    input.dispatchEvent(new CompositionEvent("compositionstart", {
      bubbles: true,
      data: "",
    }));
    start.click();
    for (const value of values) {
      setValue.call(input, value);
      input.dispatchEvent(new CompositionEvent("compositionupdate", {
        bubbles: true,
        data: value,
      }));
      input.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        data: value,
        inputType: "insertCompositionText",
      }));
      await frame();
      await frame();
      heights.push(input.getBoundingClientRect().height);
    }
    input.dispatchEvent(new CompositionEvent("compositionend", {
      bubbles: true,
      data: values.at(-1),
    }));
    typed = true;
    await monitor;
    return {
      heights,
      worstDistance: Math.max(0, ...distances),
    };
  });

  expect(result.heights.at(-1)).toBeGreaterThan(result.heights[0]);
  expect(result.heights.at(-1)).toBeLessThanOrEqual(133);
  expect(result.worstDistance).toBeLessThanOrEqual(2);
});

test("long paste stays out of the textarea and remains editable before send", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const pasted = `Editable paste opening ${"content ".repeat(170)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, pasted);
  await expect(input).toHaveValue("");
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await expect(card).toContainText("Editable paste opening");
  await expect(card).toContainText(`${pasted.length} 字符`);
  const box = await card.boundingBox();
  expect(box?.width ?? 999).toBeLessThanOrEqual(302);

  await card.locator(".paste-open").click();
  const editor = page.getByRole("dialog", { name: "编辑粘贴内容" })
    .getByRole("textbox");
  await editor.fill("edited paste body");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card).toContainText("edited paste body");
  await expect(input).toHaveValue("");

  await input.fill("visible follow-up");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.getByTestId("composer-paste-output"))
    .toHaveText("edited paste body\n\nvisible follow-up");
  await expect(card).toHaveCount(0);
  await expect(input).toHaveValue("");
});

test("oversized edited paste stays in the draft instead of being cleared", async ({
  page,
}) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1&composer-paste=1",
  );
  const input = page.locator(
    '[data-testid="live-composer-shell"] .composer textarea',
  );
  const seed = `oversized ${"seed ".repeat(205)}`;
  await input.evaluate((node, text) => {
    const data = new DataTransfer();
    data.setData("text/plain", text);
    node.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: data,
    }));
  }, seed);
  const card = page.locator(
    '[data-testid="live-composer-shell"] .paste-card',
  );
  await card.locator(".paste-open").click();
  const pasteEditor = page.getByRole("dialog", { name: "编辑粘贴内容" });
  await pasteEditor.getByRole("textbox")
    .fill("x".repeat(2 * 1024 * 1024));
  await expect(pasteEditor.locator("small"))
    .toContainText("2097152 字符 · 1 行");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(card.locator(".paste-card-preview")).toHaveText(/^x{180}$/);
  await input.fill("tail");
  await page.locator(
    '[data-testid="live-composer-shell"] .sendbtn',
  ).click();
  await expect(page.locator(
    '[data-testid="live-composer-shell"] .composer-notice',
  )).toContainText("消息内容超过上限");
  await expect(card).toHaveCount(1);
  await expect(input).toHaveValue("tail");
  await expect(page.getByTestId("composer-paste-output")).toHaveText("");
});

test("multi-line composer growth does not move a history reader", async ({
  page,
}, testInfo) => {
  await page.goto(
    "/tests/history-browser.html?codex-live-burst=1&composer-live=1",
  );
  await wheelUntilTurn(
    page, "burst-history-1", -2_000, testInfo.project.name,
  );
  await waitForScrollIdle(page);
  const before = await readingAnchor(page);
  await page.locator(
    '[data-testid="live-composer-shell"] textarea',
  ).fill([
    "第一行输入",
    "第二行输入",
    "第三行输入",
    "第四行输入",
    "第五行输入",
  ].join("\n"));
  await page.waitForTimeout(120);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

test("composer action growth keeps the live tail visible without stealing history", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/history-browser.html?large=40&composer-resize=1");
  const viewport = page.locator(".thread");
  await expect(page.locator('[data-turn-id="m40"]')).toBeVisible();
  await viewport.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);

  await page.getByTestId("toggle-composer").click();
  await expect.poll(async () => viewport.evaluate((node) =>
    node.scrollHeight - node.scrollTop - node.clientHeight,
  )).toBeLessThan(2);
  const spark = page.locator('[data-turn-id="m40"] .turn-done-mark');
  await expect(spark).toBeVisible();
  expect(await spark.evaluate((node) => {
    const viewportNode = document.querySelector<HTMLElement>(".thread");
    if (!viewportNode) throw new Error("thread viewport is missing");
    return node.getBoundingClientRect().bottom
      <= viewportNode.getBoundingClientRect().bottom + 1;
  })).toBe(true);

  await wheelUntilTurn(page, "m1", -2_000, testInfo.project.name);
  const before = await readingAnchor(page);
  await page.getByTestId("toggle-composer").click();
  await page.waitForTimeout(200);
  const after = await readingAnchor(page);
  expect(after.id).toBe(before.id);
  expect(Math.abs(after.offset - before.offset)).toBeLessThan(2);
});

for (const width of [320, 390]) {
  test(`Codex controls stay on one row in a ${width} px composer`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 720 });
    await page.goto("/tests/history-browser.html?quota-composer=1");

    const composer = page.getByTestId("quota-composer");
    const input = composer.getByRole("textbox", { name: "message" });
    await expect(input).toHaveAttribute(
      "placeholder", "输入 / 命令，$ Skill");
    await expect(composer.locator(".fast-chip")).toHaveText("快速");
    await expect(composer.locator(".fast-chip")).not.toContainText("⚡");
    await expect(composer.locator(".usage-meter")).toBeVisible();
    await expect(composer.locator(".hint-ring")).toBeVisible();
    const inputHeight = await input.evaluate((node) => ({
      client: node.clientHeight,
      scroll: node.scrollHeight,
    }));
    expect(inputHeight.scroll).toBeLessThanOrEqual(inputHeight.client + 1);

    const layout = await composer.evaluate((node) => {
      const footer = node.getBoundingClientRect();
      const controls = [
        node.querySelector<HTMLElement>(".hint-mode"),
        ...node.querySelectorAll<HTMLElement>(".hint-right > .hint-ctl"),
        node.querySelector<HTMLElement>(".usage-meter"),
        node.querySelector<HTMLElement>(".hint-ring"),
      ].filter((control): control is HTMLElement => control !== null)
        .map((control) => {
          const rect = control.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            width: rect.width,
            centerY: rect.top + rect.height / 2,
          };
        });
      return {
        clientWidth: node.clientWidth,
        scrollWidth: node.scrollWidth,
        footerLeft: footer.left,
        footerRight: footer.right,
        controls,
      };
    });
    expect(layout.controls).toHaveLength(6);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    expect(Math.max(...layout.controls.map((control) => control.centerY))
      - Math.min(...layout.controls.map((control) => control.centerY)))
      .toBeLessThanOrEqual(2);
    const minimumWidths = [48, 48, 28, 20, 44, 28];
    for (const [index, control] of layout.controls.entries()) {
      expect(control.width).toBeGreaterThanOrEqual(minimumWidths[index]);
    }
    for (const [index, control] of layout.controls.entries()) {
      expect(control.left).toBeGreaterThanOrEqual(layout.footerLeft);
      expect(control.right).toBeLessThanOrEqual(layout.footerRight);
      if (index > 0) {
        expect(control.left - layout.controls[index - 1].right)
          .toBeGreaterThanOrEqual(10);
      }
    }
  });
}

test("queued messages expand to full editable prompts", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto("/tests/history-browser.html?queued-query-editor=1");

  const fixture = page.getByTestId("queued-query-fixture");
  const preview = fixture.locator(".qt");
  await expect(preview).toBeVisible();
  await expect(fixture).not.toContainText("QUEUED-INSTRUCTION-END");
  expect(await preview.evaluate((node) =>
    node.scrollWidth > node.clientWidth)).toBe(true);

  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  const dialog = page.getByRole("dialog", { name: "排队消息详情" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toContainText("QUEUED-INSTRUCTION-END");
  await expect(dialog).toContainText("编辑文字不会移除附件");

  await dialog.getByRole("button", { name: "编辑", exact: true }).click();
  const editor = dialog.getByRole("textbox", { name: "编辑排队消息" });
  await editor.fill("Updated queued instruction\nwith the complete context.");
  await dialog.getByRole("button", { name: "保存修改" }).click();
  await expect(dialog.getByTestId("queued-full-prompt"))
    .toHaveText("Updated queued instruction\nwith the complete context.");

  await dialog.getByRole(
    "button", { name: "关闭排队消息详情" },
  ).click();
  await expect(dialog).not.toBeVisible();
  await expect(preview).toContainText("Updated queued instruction");
  await fixture.getByRole("button", { name: "查看排队消息" }).click();
  await expect(page.getByTestId("queued-full-prompt"))
    .toContainText("with the complete context.");
  expect(await page.evaluate(() => (
    document.documentElement.scrollWidth <= document.documentElement.clientWidth
  ))).toBe(true);
});

test("migration picker cannot confirm a stale directory", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/current");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/current");
});

test("migration picker waits for its null-path response", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker-null=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(dialog.locator(".dp-crumbs")).toHaveText("…");
  await expect(dialog).toContainText("正在读取目录");
  await expect(dialog).not.toContainText("stale-child");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("<home>");

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/home/fixture");
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/home/fixture");
});

test("migration picker follows an external session move", async ({ page }) => {
  await page.goto("/tests/history-browser.html?migration-picker=1");
  await page.getByTestId("open-migration-picker").click();

  const dialog = page.getByRole("dialog", { name: "迁移 Codex 会话" });
  const confirm = dialog.getByRole(
    "button", { name: "迁移到此目录" },
  );
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/current");
  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(confirm).toBeEnabled();

  await page.getByTestId("externally-migrate-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog.locator(".dp-crumbs")).toHaveText("/repo/external");
  await expect(dialog).toContainText("正在读取目录");
  await expect(confirm).toBeDisabled();
  await expect(page.getByTestId("migration-picker-request"))
    .toHaveText("/repo/external");
  await expect(page.getByTestId("migration-picker-confirmed")).toBeEmpty();

  await page.getByTestId("resolve-migration-picker")
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(dialog).not.toContainText("正在读取目录");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId("migration-picker-confirmed"))
    .toHaveText("/repo/external");
});

async function chooseDangerousNewChatControls(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /On Request/ }).click();
  await dialog.getByRole("button", { name: /Full Access/ }).click();
  await dialog.getByRole("button", { name: "Live", exact: true }).click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });
  await expect(dialog).not.toHaveClass(/(?:^|\s)show(?:\s|$)/);
  await expect(page.locator(".newchat-access")).toContainText("Full Access");
}

let newChatSubmissionSequence = 0;

async function submitNewChatFixture(
  page: import("@playwright/test").Page,
): Promise<Record<string, unknown>> {
  const prompt = `verify scoped controls ${++newChatSubmissionSequence}`;
  await page.locator(".newchat-input").fill(prompt);
  await page.getByRole("button", { name: "开始", exact: true }).click();
  await expect(page.getByTestId("newchat-submission")).toContainText(prompt);
  return JSON.parse(
    await page.getByTestId("newchat-submission").innerText(),
  ) as Record<string, unknown>;
}

test("new-chat controls reset across device authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-device").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-b:code:codex");
  await expect(page.locator(".newchat-access")).toContainText("默认环境");

  const submitted = await submitNewChatFixture(page);
  expect(submitted.permissionMode).toBe("never");
  expect(submitted).not.toHaveProperty("permissionProfile");
  expect(submitted).not.toHaveProperty("webSearch");
});

test("new-chat controls reset across engine authorization scopes", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:code:claude");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const claudeSubmission = await submitNewChatFixture(page);
  expect(claudeSubmission).not.toHaveProperty("permissionMode");

  await page.getByTestId("switch-newchat-engine").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
  const codexSubmission = await submitNewChatFixture(page);
  expect(codexSubmission.permissionMode).toBe("never");
  expect(codexSubmission).not.toHaveProperty("permissionProfile");
  expect(codexSubmission).not.toHaveProperty("webSearch");
});

test("new-chat controls reset and normalize across Code and Work", async ({
  page,
}) => {
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await chooseDangerousNewChatControls(page);

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.getByTestId("newchat-scope"))
    .toHaveText("machine-a:work:codex");
  await expect(page.locator(".newchat-access")).toHaveCount(0);
  const workSubmission = await submitNewChatFixture(page);
  expect(workSubmission.permissionMode).toBe("never");
  expect(workSubmission).not.toHaveProperty("permissionProfile");
  expect(workSubmission).not.toHaveProperty("webSearch");

  await page.getByTestId("switch-newchat-space").click();
  await expect(page.locator(".newchat-access")).toContainText("默认环境");
});

test("a 256-character profile id fits a 296 px new-chat row", async ({
  page,
}) => {
  await page.setViewportSize({ width: 296, height: 720 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&long-profile=1",
  );
  await page.locator(".newchat-access").click();
  const customProfile = page.getByRole("dialog", {
    name: "权限与执行环境",
  }).locator('button[title^="custom-profile-"]');
  const profileRow = await customProfile.evaluate((button) => {
    const sheet = button.closest<HTMLElement>(".sheet");
    const name = button.querySelector<HTMLElement>(".cmd-nm");
    if (!sheet || !name) {
      throw new Error("permission-profile sheet row is incomplete");
    }
    const rowRect = button.getBoundingClientRect();
    const sheetRect = sheet.getBoundingClientRect();
    const nameStyle = getComputedStyle(name);
    return {
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetClientWidth: sheet.clientWidth,
      sheetScrollWidth: sheet.scrollWidth,
      rowLeft: rowRect.left,
      rowRight: rowRect.right,
      sheetLeft: sheetRect.left,
      sheetRight: sheetRect.right,
      nameClientWidth: name.clientWidth,
      nameScrollWidth: name.scrollWidth,
      nameOverflow: nameStyle.overflow,
      nameTextOverflow: nameStyle.textOverflow,
      titleLength: Array.from(button.getAttribute("title") ?? "").length,
    };
  });
  expect(profileRow.pageScrollWidth).toBeLessThanOrEqual(
    profileRow.viewportWidth);
  expect(profileRow.sheetScrollWidth).toBeLessThanOrEqual(
    profileRow.sheetClientWidth);
  expect(profileRow.rowLeft).toBeGreaterThanOrEqual(profileRow.sheetLeft);
  expect(profileRow.rowRight).toBeLessThanOrEqual(profileRow.sheetRight);
  expect(profileRow.nameScrollWidth).toBeGreaterThan(
    profileRow.nameClientWidth);
  expect(profileRow.nameOverflow).toBe("hidden");
  expect(profileRow.nameTextOverflow).toBe("ellipsis");
  expect(profileRow.titleLength).toBe(256);
  await customProfile.click();
  await page.locator(".scrim.show").click({ position: { x: 8, y: 8 } });

  const layout = await page.getByTestId("newchat-controls-fixture")
    .evaluate((node) => {
      const card = node.querySelector<HTMLElement>(".newchat-card");
      const access = node.querySelector<HTMLElement>(".newchat-access");
      if (!card || !access) throw new Error("new-chat controls are missing");
      const cardRect = card.getBoundingClientRect();
      const accessRect = access.getBoundingClientRect();
      return {
        fixtureClientWidth: node.clientWidth,
        fixtureScrollWidth: node.scrollWidth,
        cardClientWidth: card.clientWidth,
        cardScrollWidth: card.scrollWidth,
        accessLeft: accessRect.left,
        accessRight: accessRect.right,
        cardLeft: cardRect.left,
        cardRight: cardRect.right,
        label: access.textContent ?? "",
      };
    });
  expect(layout.fixtureScrollWidth).toBeLessThanOrEqual(
    layout.fixtureClientWidth);
  expect(layout.cardScrollWidth).toBeLessThanOrEqual(layout.cardClientWidth);
  expect(layout.accessLeft).toBeGreaterThanOrEqual(layout.cardLeft);
  expect(layout.accessRight).toBeLessThanOrEqual(layout.cardRight);
  expect(layout.label).toContain("…");
  expect(Array.from(layout.label).length).toBeLessThan(32);
});

test("new-chat controls fit the default permission picker on a short phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const optionButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".permission-options .cmd"),
    );
    const searchButtons = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    );
    const live = searchButtons.find((button) => button.textContent === "Live");
    if (!scroll || optionButtons.length !== 6 || !live) {
      throw new Error("compact permission controls are incomplete");
    }
    const sheetRect = sheet.getBoundingClientRect();
    return {
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      sheetTop: sheetRect.top,
      sheetBottom: sheetRect.bottom,
      scrollTop: scroll.scrollTop,
      scrollHeight: scroll.scrollHeight,
      scrollClientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      minOptionHeight: Math.min(...optionButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
      minSearchHeight: Math.min(...searchButtons.map(
        (button) => button.getBoundingClientRect().height,
      )),
    };
  });

  expect(layout.scrollTop).toBe(0);
  expect(layout.scrollHeight).toBeLessThanOrEqual(
    layout.scrollClientHeight + 1,
  );
  expect(layout.sheetTop).toBeGreaterThanOrEqual(0);
  expect(layout.sheetBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.viewportHeight + 1);
  expect(layout.minOptionHeight).toBeGreaterThanOrEqual(44);
  expect(layout.minSearchHeight).toBeGreaterThanOrEqual(44);
  expect(layout.pageScrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
});

test("new-chat controls keep scrolling for many custom permission profiles", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 400 });
  await page.goto(
    "/tests/history-browser.html?newchat-controls=1&many-profiles=1",
  );
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const scrollState = await dialog.locator(".sheet-scroll").evaluate((scroll) => ({
    clientHeight: scroll.clientHeight,
    scrollHeight: scroll.scrollHeight,
    overflowY: getComputedStyle(scroll).overflowY,
  }));
  expect(scrollState.scrollHeight).toBeGreaterThan(scrollState.clientHeight);
  expect(scrollState.overflowY).toBe("auto");

  const live = dialog.getByRole("button", { name: "Live", exact: true });
  await live.scrollIntoViewIfNeeded();
  await expect(live).toBeInViewport();
});

test("new-chat controls fit when the visual app height is keyboard-sized", async ({
  page,
}) => {
  await page.setViewportSize({ width: 393, height: 852 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/tests/history-browser.html?newchat-controls=1");
  await page.evaluate(() => {
    document.documentElement.style.setProperty("--app-height", "400px");
    document.documentElement.style.setProperty("--keyboard-inset", "452px");
    document.documentElement.setAttribute("data-short-viewport", "true");
  });
  await page.locator(".newchat-access").click();
  const dialog = page.getByRole("dialog", {
    name: "权限与执行环境",
  });
  await expect(dialog).toBeVisible();

  const layout = await dialog.evaluate((sheet) => {
    const scroll = sheet.querySelector<HTMLElement>(".sheet-scroll");
    const live = Array.from(
      sheet.querySelectorAll<HTMLElement>(".cmd-search button"),
    ).find((button) => button.textContent === "Live");
    if (!scroll || !live) throw new Error("permission search controls missing");
    return {
      scrollHeight: scroll.scrollHeight,
      clientHeight: scroll.clientHeight,
      liveBottom: live.getBoundingClientRect().bottom,
      sheetBottom: sheet.getBoundingClientRect().bottom,
    };
  });
  expect(layout.scrollHeight).toBeLessThanOrEqual(layout.clientHeight + 1);
  expect(layout.liveBottom).toBeLessThanOrEqual(layout.sheetBottom + 1);
});

test("Work multi-account controls filter labels and seed a new immutable owner", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=work");

  await expect(page.getByRole("group", {
    name: "筛选 Codex 账号",
  })).toBeVisible();
  await expect(page.locator(".scard-profile-ribbon")).toHaveCount(2);

  await page.getByRole("button", { name: "nyx · Stack" }).click();
  await expect(page.locator(".scard")).toHaveCount(1);
  await expect(page.locator(".scard-profile-ribbon")).toHaveText("nyx");
  await page.getByRole("button", { name: "新工作" }).click();
  await expect(page.getByTestId("new-work-profile")).toHaveText("stack");

  await page.getByRole("tab", { name: "Code" }).click();
  await expect(page.getByRole("button", { name: "全部" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(2);

  await page.getByRole("tab", { name: "Work" }).click();
  await expect(page.getByRole("button", { name: "nyx · Stack" })).toHaveClass(/active/);
  await expect(page.locator(".scard")).toHaveCount(1);

  await page.getByRole("button", { name: "全部" }).click();
  await expect(page.locator(".scard")).toHaveCount(2);
});

test("profile keycaps hang from session cards without shifting titles", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=1");

  const activeCard = page.locator(".scard.active");
  await expect(activeCard).toBeVisible();
  const geometry = await activeCard.evaluate((card) => {
    const keycap = card.querySelector<HTMLElement>(".scard-profile-ribbon");
    const title = card.querySelector<HTMLElement>(".scard-title");
    const preview = card.querySelector<HTMLElement>(".scard-prev");
    if (!keycap || !title || !preview) {
      throw new Error("profile sidebar fixture is incomplete");
    }
    const cardRect = card.getBoundingClientRect();
    const keycapRect = keycap.getBoundingClientRect();
    const titleRect = title.getBoundingClientRect();
    const previewRect = preview.getBoundingClientRect();
    return {
      cardLeft: cardRect.left,
      cardTop: cardRect.top,
      keycapTop: keycapRect.top,
      keycapBottom: keycapRect.bottom,
      keycapWidth: keycapRect.width,
      titleLeft: titleRect.left,
      titleTop: titleRect.top,
      previewLeft: previewRect.left,
      position: getComputedStyle(keycap).position,
    };
  });

  expect(geometry.position).toBe("absolute");
  expect(geometry.keycapTop).toBeLessThan(geometry.cardTop);
  expect(geometry.keycapBottom).toBeGreaterThan(geometry.cardTop);
  expect(geometry.keycapBottom).toBeLessThanOrEqual(geometry.titleTop);
  expect(geometry.keycapWidth).toBeLessThanOrEqual(64);
  expect(geometry.titleLeft - geometry.cardLeft).toBeLessThanOrEqual(18);
  expect(Math.abs(geometry.titleLeft - geometry.previewLeft)).toBeLessThanOrEqual(1);

  const ordinaryCard = page.locator(".scard").filter({
    hasText: "cc-remote 派生",
  });
  const ordinaryGeometry = await ordinaryCard.evaluate((card) => {
    const title = card.querySelector<HTMLElement>(".scard-title");
    if (!title) throw new Error("ordinary profile title missing");
    const style = getComputedStyle(card);
    return {
      titleInset:
        title.getBoundingClientRect().left - card.getBoundingClientRect().left,
      borderColor: style.borderTopColor,
      backgroundColor: style.backgroundColor,
    };
  });
  expect(ordinaryGeometry.titleInset).toBeLessThanOrEqual(18);
  expect(ordinaryGeometry.borderColor).not.toBe("transparent");
  expect(ordinaryGeometry.borderColor).not.toBe("rgba(0, 0, 0, 0)");
  expect(ordinaryGeometry.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
});

test("profile session card edges remain visible in dark theme", async ({
  page,
}) => {
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/tests/history-browser.html?profile-sidebar=1&theme=dark");
  await page.waitForFunction(() =>
    document.documentElement.dataset.theme === "dark"
  );
  await page.waitForTimeout(200);

  const appearance = await page.locator(".scard").evaluateAll((cards) => {
    const sample = (color: string) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas context unavailable");
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = color;
      context.fillRect(0, 0, 1, 1);
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    };
    const channelDelta = (left: number[], right: number[]) =>
      Math.max(...left.slice(0, 3).map((value, index) =>
        Math.abs(value - right[index])
      ));
    const sidebar = sample(getComputedStyle(document.documentElement)
      .getPropertyValue("--sidebar"));
    return cards.map((card) => {
      const style = getComputedStyle(card);
      const border = sample(style.borderTopColor);
      const background = sample(style.backgroundColor);
      return {
        active: card.classList.contains("active"),
        borderAlpha: border[3],
        borderCardDelta: channelDelta(border, background),
        borderSidebarDelta: channelDelta(border, sidebar),
        background: style.backgroundColor,
        boxShadow: style.boxShadow,
      };
    });
  });

  expect(appearance).toHaveLength(2);
  for (const card of appearance) {
    expect(card.borderAlpha).toBe(255);
    expect(card.borderCardDelta).toBeGreaterThanOrEqual(20);
    expect(card.borderSidebarDelta).toBeGreaterThanOrEqual(20);
  }
  expect(appearance.some((card) => card.active)).toBe(true);
  expect(appearance.some((card) => !card.active)).toBe(true);
  const active = appearance.find((card) => card.active)!;
  const inactive = appearance.find((card) => !card.active)!;
  expect(active.background).not.toBe(inactive.background);
  expect(active.boxShadow).not.toBe("none");
});
