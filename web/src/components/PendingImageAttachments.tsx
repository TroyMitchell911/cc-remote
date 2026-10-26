import { useState } from "react";
import type { QueryImg } from "../protocol";
import { Icon } from "../icons";
import { ImageLightbox } from "./ImageLightbox";

function imageSource(image: QueryImg): string {
  return `data:${image.media_type};base64,${image.data}`;
}

export function PendingImageAttachments({ images, onRemove }: {
  images: QueryImg[];
  onRemove: (index: number) => void;
}) {
  const [preview, setPreview] = useState<{
    index: number;
    src: string;
    alt: string;
  } | null>(null);
  const activeImage = preview ? images[preview.index] : undefined;
  const activePreview = preview && activeImage
    && imageSource(activeImage) === preview.src
    ? preview
    : null;

  return <>
    {images.map((image, index) => {
      const src = imageSource(image);
      const alt = `待发送图片 ${index + 1}`;
      return (
        <span key={index} className="attach-img">
          <button type="button" className="attach-image-preview"
            aria-label={`预览${alt}`} onClick={() => setPreview({ index, src, alt })}>
            <img src={src} alt={alt} />
          </button>
          <button type="button" className="attach-x"
            onClick={() => {
              if (activePreview?.index === index) setPreview(null);
              onRemove(index);
            }}
            aria-label={`移除${alt}`}>
            <Icon name="close" size={12} />
          </button>
        </span>
      );
    })}
    {activePreview && (
      <ImageLightbox src={activePreview.src} alt={activePreview.alt}
        imageKey={String(activePreview.index)}
        navigation={images.length > 1 ? {
          index: activePreview.index, count: images.length,
          onSelect: (index) => setPreview({
            index, src: imageSource(images[index]), alt: `待发送图片 ${index + 1}`,
          }),
        } : undefined}
        onClose={() => setPreview(null)} />
    )}
  </>;
}
