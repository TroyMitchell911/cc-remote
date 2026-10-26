import { useCallback, useMemo, useRef, useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import { PendingImageAttachments } from "../../src/components/PendingImageAttachments";
import { PlanProgressContent } from "../../src/components/PlanProgressPopover";
import { claudePlanProgress } from "../../src/plan-progress";
import { historyImageAssetKey, HISTORY_IMAGE_REQUEST_TIMEOUT_MS, type HistoryImageAsset, type HistoryImageVariant } from "../../src/history-image-assets";
import type { InlineImageAsset } from "../../src/inline-image-assets";
import type { Turn } from "../../src/domain/conversation";
import type { QueryImg } from "../../src/protocol";

const noop = () => {};
export function ImageGalleryFixture() {
  const images = useMemo((): QueryImg[] => ["#d17758", "#496baf", "#319b78"].map((color) => {
    const canvas = document.createElement("canvas");
    canvas.width = 800;
    canvas.height = 1000;
    const context = canvas.getContext("2d")!;
    context.fillStyle = color;
    context.fillRect(0, 0, canvas.width, canvas.height);
    return { media_type: "image/png", data: canvas.toDataURL().split(",")[1] };
  }), []);
  const ready = useCallback((index: number): HistoryImageAsset => ({
    status: "ready", data: images[index].data, mediaType: "image/png", width: 800, height: 1000,
  }), [images]);
  const [pending, setPending] = useState(images);
  const [sid, setSid] = useState("gallery");
  const [dismissed, setDismissed] = useState(false);
  const [requests, setRequests] = useState<string[]>([]);
  const historyRef = useRef<Record<string, HistoryImageAsset>>({
    [historyImageAssetKey("history-native", "upload", "thumbnail")]: ready(0),
  });
  const [historyAssets, setHistoryAssets] = useState(historyRef.current);
  const [liveAssets, setLiveAssets] = useState<Record<string, InlineImageAsset>>({
    generated: ready(2), unavailable: { status: "error", error: "fixture load failed" },
  });
  const liveAttempts = useRef(0);
  const historyAttempts = useRef(0);
  const onLoadHistory = useCallback((turnId: string, imageId: string, variant: HistoryImageVariant) => {
    const key = historyImageAssetKey(turnId, imageId, variant);
    const previous = historyRef.current[key];
    if (previous?.status === "ready" || (previous?.status === "loading"
        && Date.now() - (previous.startedAt ?? 0) < HISTORY_IMAGE_REQUEST_TIMEOUT_MS)) return true;
    setRequests((values) => [...values, `${turnId}/${imageId}/${variant}`]);
    const attempt = ++historyAttempts.current;
    const failure = attempt === 1 ? new URLSearchParams(location.search).get("original-failure") : null;
    historyRef.current = { ...historyRef.current, [key]: {
      status: "loading", startedAt: Date.now(), requestGeneration: attempt,
    } };
    setHistoryAssets(historyRef.current);
    if (failure === "timeout") return true;
    window.setTimeout(() => {
      historyRef.current = { ...historyRef.current, [key]: failure === "error"
        ? { status: "error", error: "fixture load failed" } : ready(1) };
      setHistoryAssets(historyRef.current);
    }, 600);
    return true;
  }, [ready]);
  const onLoadImage = useCallback((_path: string, previewId?: string) => {
    setRequests((values) => [...values, `live/${previewId}`]);
    // A failed load must stay navigable and explicitly retryable.
    const asset = ++liveAttempts.current === 1 ? { status: "error" as const } : ready(0);
    setLiveAssets((values) => ({ ...values, [previewId!]: asset }));
    return true;
  }, [ready]);
  const turns = useMemo((): Turn[] => [{
    id: "uploads", prompt: "上传的图片", done: true, images: images.slice(0, 2), blocks: [],
  }, {
    id: "history", historyTurnId: "history-native", prompt: "历史图片", done: true,
    imageRefs: [{ image_id: "upload", width: 800, height: 1000, byte_size: 200, media_type: "image/png" }], blocks: [],
  }, {
    id: "generated-turn", prompt: "生成图片", done: true,
    blocks: ["generated", "unavailable"].map((id) => ({
      kind: "process", processKind: "mcp", tool: "image_generation", item_id: id,
      title: "生成图片", phase: "end", status: "succeeded", done: true,
      input: { path: `/tmp/${id}.png`, preview_id: id },
    })),
  }], [images]);
  if (new URLSearchParams(location.search).has("plan-header")) {
    const plan = claudePlanProgress({ id: "plan", revision: 1, turn_id: "turn", phase: "approved",
      document: "# 验证计划\n\n" + "这是计划的正文。\n\n".repeat(60), steps: [], dismissed: false })!;
    return <main data-theme="dark" data-engine="claude" style={{ padding: 14 }}>
      {!dismissed && <section className="plan-progress-popover" style={{ position: "relative", maxHeight: 560, transform: "none" }}>
        <PlanProgressContent block={plan.block} onDismiss={() => setDismissed(true)} />
      </section>}
    </main>;
  }
  return <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <button onClick={() => setSid("other")}>Switch session</button>
    <output data-testid="image-requests">{requests.join(",")}</output>
    <ChatView sid={sid} turns={sid === "gallery" ? turns : []} loading={false} hasMore={false}
      onLoadMore={noop} imageAssets={liveAssets} historyImageAssets={historyAssets}
      onLoadHistoryImage={onLoadHistory} onLoadImage={onLoadImage} />
    <div style={{ display: "flex", flexShrink: 0 }}>
      <PendingImageAttachments images={pending} onRemove={(index) => setPending((items) => items.filter((_, i) => i !== index))} />
    </div>
  </main>;
}
