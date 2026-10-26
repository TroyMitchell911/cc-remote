import { useEffect, useRef, useState } from "react";
import type { ChatImageSelection } from "../chat-image-gallery";
import {
  historyImageAssetKey, readyGeneratedImageAsset, HISTORY_IMAGE_REQUEST_TIMEOUT_MS,
  type HistoryImageAsset, type HistoryImageVariant,
} from "../history-image-assets";
import type { InlineImageAsset } from "../inline-image-assets";
import type { PreviewAuthorizationState } from "../reducer";
import { ImageLightbox } from "./ImageLightbox";
import { PreviewAuthorizationPrompt } from "./PreviewAuthorizationPrompt";

export function ChatImageLightbox({ selection, onSelect, onClose, imageAssets,
  historyImageAssets, onLoadImage, onLoadHistoryImage, onAuthorizeImage }: {
  selection: ChatImageSelection;
  onSelect: (index: number) => void;
  onClose: () => void;
  imageAssets?: Record<string, InlineImageAsset>;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onLoadHistoryImage?: (turnId: string, imageId: string, variant: HistoryImageVariant) => boolean;
  onAuthorizeImage?: (authorization: PreviewAuthorizationState, decision: "allow" | "deny") => boolean;
}) {
  const entry = selection.images[selection.index];
  const image = entry.image;
  const history = image.kind !== "data" && image.imageId
    ? historyImageAssets?.[historyImageAssetKey(image.turnId, image.imageId, "full")]
    : undefined;
  const generated = image.kind === "generated" && image.imageId
    ? readyGeneratedImageAsset(historyImageAssets, image.imageId) : undefined;
  const live = image.kind === "generated" && image.previewId
    ? imageAssets?.[image.previewId] : undefined;
  const thumbnail = image.kind === "history"
    ? historyImageAssets?.[historyImageAssetKey(image.turnId, image.imageId, "thumbnail")]
    : undefined;
  const full = generated ?? (live?.status === "ready" ? live : history ?? live);
  const asset = full?.status === "ready" ? full : thumbnail;
  const src = image.kind === "data" ? image.src
    : asset?.status === "ready" && asset.data && asset.mediaType
      ? `data:${asset.mediaType};base64,${asset.data}`
      : undefined;
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const [stalled, setStalled] = useState(false);
  const loadRef = useRef<(retry?: boolean) => boolean>(() => false);
  loadRef.current = (retry = false) => {
    if (image.kind === "data" || full?.status === "ready") return true;
    if (!retry && (full?.status === "loading" || full?.status === "authorization")) return true;
    if (image.imageId) return onLoadHistoryImage?.(image.turnId, image.imageId, "full") ?? false;
    return image.kind === "generated" && image.previewId
      ? onLoadImage?.(image.path, image.previewId) ?? false : false;
  };
  useEffect(() => {
    // One request per selected image; cache updates/evictions cannot start a
    // retry loop or prefetch the rest of a long conversation.
    setFailedKey(loadRef.current() ? null : entry.key);
  }, [entry.key]);
  useEffect(() => {
    setStalled(false);
    if (image.kind === "data" || full?.status === "ready" || full?.status === "error"
        || full?.status === "authorization") return;
    const timer = window.setTimeout(() => setStalled(true), HISTORY_IMAGE_REQUEST_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [entry.key, image.kind, full?.status, full?.requestGeneration]);
  const failed = failedKey === entry.key || stalled || full?.status === "error";
  const retry = () => {
    setStalled(false);
    setFailedKey(loadRef.current(true) ? null : entry.key);
  };
  return <ImageLightbox src={src} alt={image.alt} imageKey={entry.key} onClose={onClose}
    navigation={selection.images.length > 1 ? {
      index: selection.index, count: selection.images.length, onSelect,
    } : undefined}
    placeholder={full?.status === "authorization" && full.authorization
      ? <PreviewAuthorizationPrompt authorization={full.authorization} onDecision={onAuthorizeImage} />
      : <div className="image-lightbox-status" role="status">
          <span>{failed ? "图片加载失败" : "正在加载图片…"}</span>
          {failed && <button type="button" onClick={retry}>重试</button>}
        </div>} />;
}
