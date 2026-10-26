export interface ImagePoint { x: number; y: number }
export interface ImageSize { width: number; height: number }
export interface ImageTransform { scale: number; x: number; y: number }

export const MIN_IMAGE_SCALE = 1;
export const MAX_IMAGE_SCALE = 4;

/** A deliberate horizontal drag at fitted size turns the page. Zoomed images
 * reserve the same gesture for panning; short/vertical drags stay on the image. */
export function imageSwipeDirection(
  deltaX: number, deltaY: number, viewportWidth: number, scale: number,
): -1 | 0 | 1 {
  const threshold = Math.max(48, Math.min(80, viewportWidth * 0.15));
  if (scale > MIN_IMAGE_SCALE || Math.abs(deltaX) < threshold
      || Math.abs(deltaX) < Math.abs(deltaY) * 1.25) return 0;
  return deltaX < 0 ? 1 : -1;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function constrainImageTransform(
  transform: ImageTransform,
  image: ImageSize,
  viewport: ImageSize,
): ImageTransform {
  const scale = clamp(transform.scale, MIN_IMAGE_SCALE, MAX_IMAGE_SCALE);
  if (scale <= MIN_IMAGE_SCALE) return { scale: MIN_IMAGE_SCALE, x: 0, y: 0 };
  const maxX = Math.max(0, (image.width * scale - viewport.width) / 2);
  const maxY = Math.max(0, (image.height * scale - viewport.height) / 2);
  return {
    scale,
    x: clamp(transform.x, -maxX, maxX),
    y: clamp(transform.y, -maxY, maxY),
  };
}

export function panImageTransform(
  start: ImageTransform,
  deltaX: number,
  deltaY: number,
  image: ImageSize,
  viewport: ImageSize,
): ImageTransform {
  return constrainImageTransform({
    scale: start.scale,
    x: start.x + deltaX,
    y: start.y + deltaY,
  }, image, viewport);
}

export function zoomImageTransform(
  start: ImageTransform,
  scaleFactor: number,
  focalPoint: ImagePoint,
  image: ImageSize,
  viewport: ImageSize,
): ImageTransform {
  if (start.scale <= 0 || !Number.isFinite(scaleFactor)) {
    return constrainImageTransform(start, image, viewport);
  }
  const scale = clamp(
    start.scale * scaleFactor,
    MIN_IMAGE_SCALE,
    MAX_IMAGE_SCALE,
  );
  const center = { x: viewport.width / 2, y: viewport.height / 2 };
  const contentPoint = {
    x: (focalPoint.x - center.x - start.x) / start.scale,
    y: (focalPoint.y - center.y - start.y) / start.scale,
  };
  return constrainImageTransform({
    scale,
    x: focalPoint.x - center.x - scale * contentPoint.x,
    y: focalPoint.y - center.y - scale * contentPoint.y,
  }, image, viewport);
}

export function pinchImageTransform(
  start: ImageTransform,
  startFirst: ImagePoint,
  startSecond: ImagePoint,
  currentFirst: ImagePoint,
  currentSecond: ImagePoint,
  image: ImageSize,
  viewport: ImageSize,
): ImageTransform {
  const startDistance = Math.hypot(
    startSecond.x - startFirst.x,
    startSecond.y - startFirst.y,
  );
  if (startDistance <= 0) {
    return constrainImageTransform(start, image, viewport);
  }
  const currentDistance = Math.hypot(
    currentSecond.x - currentFirst.x,
    currentSecond.y - currentFirst.y,
  );
  const scale = clamp(
    start.scale * currentDistance / startDistance,
    MIN_IMAGE_SCALE,
    MAX_IMAGE_SCALE,
  );
  const startMidpoint = {
    x: (startFirst.x + startSecond.x) / 2,
    y: (startFirst.y + startSecond.y) / 2,
  };
  const currentMidpoint = {
    x: (currentFirst.x + currentSecond.x) / 2,
    y: (currentFirst.y + currentSecond.y) / 2,
  };
  const center = { x: viewport.width / 2, y: viewport.height / 2 };
  const contentPoint = {
    x: (startMidpoint.x - center.x - start.x) / start.scale,
    y: (startMidpoint.y - center.y - start.y) / start.scale,
  };
  return constrainImageTransform({
    scale,
    x: currentMidpoint.x - center.x - scale * contentPoint.x,
    y: currentMidpoint.y - center.y - scale * contentPoint.y,
  }, image, viewport);
}
