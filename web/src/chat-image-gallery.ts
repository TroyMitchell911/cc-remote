import type { Turn } from "./domain/conversation";
import { filePathsFromInput } from "./file-changes";
import { mergeDetailWithLiveTail } from "./history-merge";
import { generatedImageIdentity, generatedOutputImages } from "./process-blocks";
import type { TurnImagePreviewCache } from "./turn-image-previews";

export type ChatImageSource =
  | { kind: "data"; src: string; alt: string }
  | { kind: "history"; turnId: string; imageId: string; alt: string }
  | { kind: "generated"; turnId: string; imageId?: string; previewId?: string; path: string; alt: string };

export interface ChatGalleryEntry { key: string; image: ChatImageSource }
export interface ChatImageSelection { images: ChatGalleryEntry[]; index: number }

/** Snapshot only the currently loaded conversation's image references when a
 * preview opens. Browsing a gallery must not expand history or retain a second
 * copy of the generated/history asset caches. */
export function chatImageGallery(
  turns: readonly Turn[],
  previews: TurnImagePreviewCache,
): ChatGalleryEntry[] {
  return turns.flatMap((turn) => {
    const turnId = turn.historyTurnId ?? turn.id;
    const uploads: ChatGalleryEntry[] = turn.images?.length
      ? turn.images.map((image, index) => ({
          key: `${turn.id}:upload:${index}`,
          image: { kind: "data", src: `data:${image.media_type};base64,${image.data}`, alt: "用户发送的图片" },
        }))
      : (turn.imageRefs ?? []).map((image, index) => {
          const fallback = previews.get(turn.id, index);
          return {
            key: `${turn.id}:upload:${index}`,
            image: fallback
              ? { kind: "data", src: `data:${fallback.media_type};base64,${fallback.data}`, alt: "用户发送的图片" }
              : { kind: "history", turnId, imageId: image.image_id, alt: "用户发送的图片" },
          };
        });
    const settled = turn.done && !turn.detailRestorePending && !turn.detailRestoreIncomplete;
    const blocks = mergeDetailWithLiveTail(mergeDetailWithLiveTail(
      turn.detailProjection?.blocks ?? [], turn.liveSpillBlocks ?? [], settled,
    ), turn.blocks, settled);
    return [...uploads, ...generatedOutputImages(blocks).map((block): ChatGalleryEntry => {
      const history = block.input?.history_image as { image_id?: unknown } | undefined;
      return {
        key: `${turn.id}:generated:${generatedImageIdentity(block)}`,
        image: {
          kind: "generated", turnId, alt: "生成的图片",
          imageId: typeof history?.image_id === "string" ? history.image_id : undefined,
          previewId: typeof block.input?.preview_id === "string" ? block.input.preview_id : undefined,
          path: filePathsFromInput(block.input)[0] ?? "",
        },
      };
    })];
  });
}
