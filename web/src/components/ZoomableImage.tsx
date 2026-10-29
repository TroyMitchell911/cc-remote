import { useEffect, useRef, type ImgHTMLAttributes } from "react";
import {
  constrainImageTransform, panImageTransform, pinchImageTransform, zoomImageTransform,
  type ImagePoint, type ImageTransform,
} from "../image-gesture";

type SafariGesture = Event & { scale: number; clientX: number; clientY: number };

/** File previews share this viewport in both docked and centered presentations. */
export function ZoomableImage({ src, alt, onLoad, onError }: Pick<
  ImgHTMLAttributes<HTMLImageElement>, "src" | "alt" | "onLoad" | "onError"
>) {
  const stageRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);

  useEffect(() => {
    const stage = stageRef.current;
    const image = imageRef.current;
    if (!stage || !image) return;
    let transform: ImageTransform = { scale: 1, x: 0, y: 0 };
    let frame: number | null = null;
    const pointers = new Map<number, ImagePoint>();
    const sizes = () => ({
      image: { width: image.clientWidth, height: image.clientHeight },
      viewport: { width: stage.clientWidth, height: stage.clientHeight },
    });
    const localPoint = (event: { clientX: number; clientY: number }) => {
      const rect = stage.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    const paint = () => {
      frame = null;
      image.style.transform = `translate(${transform.x}px,${transform.y}px) scale(${transform.scale})`;
      stage.dataset.zoomed = String(transform.scale > 1);
    };
    const update = (next: ImageTransform) => {
      transform = next;
      if (frame === null) frame = requestAnimationFrame(paint);
    };
    const baseline = () => ({ transform, points: [...pointers.values()], sizes: sizes() });
    let start = baseline();
    let nativeGesture: { transform: ImageTransform; scale: number } | null = null;
    paint();

    const down = (event: PointerEvent) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      event.preventDefault();
      nativeGesture = null;
      pointers.set(event.pointerId, localPoint(event));
      stage.setPointerCapture(event.pointerId);
      start = baseline();
    };
    const move = (event: PointerEvent) => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, localPoint(event));
      const points = [...pointers.values()];
      const { image, viewport } = start.sizes;
      if (points.length >= 2 && start.points.length >= 2) {
        update(pinchImageTransform(start.transform, start.points[0], start.points[1],
          points[0], points[1], image, viewport));
      } else if (points.length === 1 && start.points.length === 1) {
        update(panImageTransform(start.transform, points[0].x - start.points[0].x,
          points[0].y - start.points[0].y, image, viewport));
      }
    };
    const finish = (event: PointerEvent) => {
      if (!pointers.delete(event.pointerId)) return;
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
      start = baseline();
    };
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      // Safari can also emit wheel events during a native gesture. One input
      // stream owns the transform, so the same pinch is never applied twice.
      if (pointers.size || nativeGesture) return;
      const { image, viewport } = sizes();
      const delta = (value: number, page: number) => value * (
        event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? page : 1);
      update(event.ctrlKey
        ? zoomImageTransform(transform, Math.exp(-delta(event.deltaY, viewport.height) * 0.002),
          localPoint(event), image, viewport)
        : panImageTransform(transform, -delta(event.deltaX, viewport.width),
          -delta(event.deltaY, viewport.height), image, viewport));
    };
    const gestureStart = (raw: Event) => {
      raw.preventDefault();
      const event = raw as SafariGesture;
      if (!pointers.size && Number.isFinite(event.scale) && event.scale > 0) {
        nativeGesture = { transform, scale: event.scale };
      }
    };
    const gestureChange = (raw: Event) => {
      raw.preventDefault();
      const event = raw as SafariGesture;
      if (!nativeGesture || pointers.size || !Number.isFinite(event.scale) || event.scale <= 0) return;
      const { image, viewport } = sizes();
      update(zoomImageTransform(nativeGesture.transform, event.scale / nativeGesture.scale,
        localPoint(event), image, viewport));
    };
    const gestureEnd = (event: Event) => {
      event.preventDefault();
      nativeGesture = null;
    };
    const resize = new ResizeObserver(() => {
      const { image, viewport } = sizes();
      update(constrainImageTransform(transform, image, viewport));
      start = baseline();
      nativeGesture = null;
    });
    resize.observe(stage);
    resize.observe(image);
    stage.addEventListener("pointerdown", down);
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", finish);
    stage.addEventListener("pointercancel", finish);
    stage.addEventListener("lostpointercapture", finish);
    stage.addEventListener("wheel", wheel, { passive: false });
    stage.addEventListener("gesturestart", gestureStart, { passive: false });
    stage.addEventListener("gesturechange", gestureChange, { passive: false });
    stage.addEventListener("gestureend", gestureEnd, { passive: false });
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      resize.disconnect();
      stage.removeEventListener("pointerdown", down);
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerup", finish);
      stage.removeEventListener("pointercancel", finish);
      stage.removeEventListener("lostpointercapture", finish);
      for (const id of pointers.keys()) {
        if (stage.hasPointerCapture(id)) stage.releasePointerCapture(id);
      }
      stage.removeEventListener("wheel", wheel);
      stage.removeEventListener("gesturestart", gestureStart);
      stage.removeEventListener("gesturechange", gestureChange);
      stage.removeEventListener("gestureend", gestureEnd);
    };
  }, [src]);

  return <div ref={stageRef} className="artifact-image-stage" data-lock-horizontal-swipe>
    <img ref={imageRef} src={src} alt={alt} draggable={false}
      onDragStart={event => event.preventDefault()} onLoad={onLoad} onError={onError} />
  </div>;
}
