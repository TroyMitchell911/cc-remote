import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Icon } from "../icons";
import {
  panImageTransform,
  imageSwipeDirection,
  pinchImageTransform,
  zoomImageTransform,
  type ImagePoint,
  type ImageSize,
  type ImageTransform,
} from "../image-gesture";

const CLOSE_ANIMATION_MS = 170;
const DRAG_THRESHOLD = 5;
const WHEEL_END_MS = 90;
const WHEEL_ZOOM_RATE = 0.002;

interface GestureStart {
  transform: ImageTransform;
  points: Array<{ id: number; point: ImagePoint }>;
  sizes: {
    image: ImageSize;
    viewport: ImageSize;
  };
}

function transformStyle(transform: ImageTransform): string {
  return `translate(${transform.x}px,${transform.y}px) scale(${transform.scale})`;
}

interface CommonLightboxProps {
  alt: string;
  onClose: () => void;
  dialogLabel?: string;
  closeLabel?: string;
  imageKey?: string;
  navigation?: { index: number; count: number; onSelect: (index: number) => void };
  placeholder?: ReactNode;
}

type ImageLightboxProps = CommonLightboxProps & (
  | { src: string | undefined; sanitizedSvg?: never }
  | { src?: never; sanitizedSvg: string }
);

function normalizedWheelDelta(
  delta: number,
  mode: number,
  pageSize: number,
): number {
  if (mode === WheelEvent.DOM_DELTA_LINE) return delta * 16;
  if (mode === WheelEvent.DOM_DELTA_PAGE) return delta * pageSize;
  return delta;
}

export function ImageLightbox(props: ImageLightboxProps) {
  const {
    alt,
    onClose,
    dialogLabel = "图片预览",
    closeLabel = "关闭图片预览",
    navigation,
    placeholder,
  } = props;
  const sanitizedSvg = "sanitizedSvg" in props ? props.sanitizedSvg : undefined;
  const src = "src" in props ? props.src : undefined;
  const imageKey = props.imageKey ?? src ?? sanitizedSvg;
  const stageRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const vectorRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const pointers = useRef(new Map<number, ImagePoint>());
  const gestureStart = useRef<GestureStart | null>(null);
  const transformRef = useRef<ImageTransform>({ scale: 1, x: 0, y: 0 });
  const pendingTransform = useRef<ImageTransform | null>(null);
  const transformFrame = useRef<number | null>(null);
  const wheelEndTimer = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const swipe = useRef<{ start: ImagePoint; axis: "horizontal" | "vertical" | null } | null>(null);
  const navigationRef = useRef(navigation);
  navigationRef.current = navigation;
  const closeTimer = useRef<number | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [entered, setEntered] = useState(false);
  const [interacting, setInteracting] = useState(false);

  const visualElement = useCallback(
    () => imageRef.current ?? vectorRef.current,
    [],
  );

  const commitTransform = useCallback(() => {
    const next = pendingTransform.current;
    if (!next) return;
    pendingTransform.current = null;
    const visual = visualElement();
    if (visual) {
      visual.style.transform = transformStyle(next);
    }
  }, [visualElement]);

  const scheduleTransform = useCallback((next: ImageTransform) => {
    transformRef.current = next;
    pendingTransform.current = next;
    if (transformFrame.current !== null) return;
    transformFrame.current = window.requestAnimationFrame(() => {
      transformFrame.current = null;
      commitTransform();
    });
  }, [commitTransform]);

  const flushTransform = useCallback(() => {
    if (transformFrame.current !== null) {
      window.cancelAnimationFrame(transformFrame.current);
      transformFrame.current = null;
    }
    commitTransform();
  }, [commitTransform]);

  useLayoutEffect(() => {
    pointers.current.clear();
    gestureStart.current = null;
    swipe.current = null;
    if (wheelEndTimer.current !== null) {
      window.clearTimeout(wheelEndTimer.current);
      wheelEndTimer.current = null;
    }
    scheduleTransform({ scale: 1, x: 0, y: 0 });
    flushTransform();
    setInteracting(false);
  }, [imageKey, scheduleTransform, flushTransform]);

  const selectAdjacent = useCallback((direction: -1 | 1) => {
    const current = navigationRef.current;
    if (!current || closeTimer.current !== null) return;
    const index = current.index + direction;
    if (index >= 0 && index < current.count) current.onSelect(index);
  }, []);

  const requestClose = useCallback(() => {
    if (closeTimer.current !== null) return;
    setEntered(false);
    closeTimer.current = window.setTimeout(
      () => onCloseRef.current(), CLOSE_ANIMATION_MS);
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setEntered(true));
    closeRef.current?.focus({ preventScroll: true });
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") requestClose();
      if (navigationRef.current && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault();
        event.stopPropagation();
        selectAdjacent(event.key === "ArrowLeft" ? -1 : 1);
      }
    };
    window.addEventListener("keydown", keydown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", keydown);
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
      if (transformFrame.current !== null) {
        window.cancelAnimationFrame(transformFrame.current);
      }
      if (wheelEndTimer.current !== null) {
        window.clearTimeout(wheelEndTimer.current);
      }
    };
  }, [requestClose, selectAdjacent]);

  const sizes = useCallback(() => {
    const visual = visualElement();
    return {
      image: {
        width: visual?.clientWidth ?? 0,
        height: visual?.clientHeight ?? 0,
      },
      viewport: {
        width: stageRef.current?.clientWidth ?? window.innerWidth,
        height: stageRef.current?.clientHeight ?? window.innerHeight,
      },
    };
  }, [visualElement]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const finishWheel = () => {
      wheelEndTimer.current = null;
      flushTransform();
      setInteracting(false);
    };
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (pointers.current.size > 0) return;
      const current = transformRef.current;
      const currentSizes = sizes();
      let next: ImageTransform | null = null;
      if (event.ctrlKey) {
        const rect = stage.getBoundingClientRect();
        const deltaY = normalizedWheelDelta(
          event.deltaY,
          event.deltaMode,
          currentSizes.viewport.height,
        );
        next = zoomImageTransform(
          current,
          Math.exp(-deltaY * WHEEL_ZOOM_RATE),
          { x: event.clientX - rect.left, y: event.clientY - rect.top },
          currentSizes.image,
          currentSizes.viewport,
        );
      } else if (current.scale > 1) {
        const deltaX = normalizedWheelDelta(
          event.deltaX,
          event.deltaMode,
          currentSizes.viewport.width,
        );
        const deltaY = normalizedWheelDelta(
          event.deltaY,
          event.deltaMode,
          currentSizes.viewport.height,
        );
        next = panImageTransform(
          current,
          -deltaX,
          -deltaY,
          currentSizes.image,
          currentSizes.viewport,
        );
      }
      if (!next) return;
      setInteracting(true);
      scheduleTransform(next);
      if (wheelEndTimer.current !== null) {
        window.clearTimeout(wheelEndTimer.current);
      }
      wheelEndTimer.current = window.setTimeout(finishWheel, WHEEL_END_MS);
    };
    stage.addEventListener("wheel", wheel, { passive: false });
    return () => {
      stage.removeEventListener("wheel", wheel);
      if (wheelEndTimer.current !== null) {
        window.clearTimeout(wheelEndTimer.current);
        wheelEndTimer.current = null;
      }
    };
  }, [flushTransform, scheduleTransform, sizes]);

  const resetGestureStart = () => {
    gestureStart.current = {
      transform: transformRef.current,
      points: Array.from(pointers.current, ([id, point]) => ({ id, point })),
      sizes: sizes(),
    };
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    if (pointers.current.size === 0) {
      suppressClick.current = false;
      if (wheelEndTimer.current !== null) {
        window.clearTimeout(wheelEndTimer.current);
        wheelEndTimer.current = null;
      }
      setInteracting(true);
      swipe.current = navigation && navigation.count > 1 && transformRef.current.scale === 1
        ? { start: { x: event.clientX, y: event.clientY }, axis: null } : null;
    }
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size > 1) {
      suppressClick.current = true;
      if (swipe.current) scheduleTransform({ scale: 1, x: 0, y: 0 });
      swipe.current = null;
    }
    resetGestureStart();
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const start = gestureStart.current;
    if (!start) return;
    const current = Array.from(pointers.current, ([id, point]) => ({ id, point }));
    const { image, viewport } = start.sizes;
    if (current.length >= 2 && start.points.length >= 2) {
      suppressClick.current = true;
      scheduleTransform(pinchImageTransform(
        start.transform,
        start.points[0].point,
        start.points[1].point,
        current[0].point,
        current[1].point,
        image,
        viewport,
      ));
      return;
    }
    if (current.length !== 1 || start.points.length !== 1) return;
    const deltaX = current[0].point.x - start.points[0].point.x;
    const deltaY = current[0].point.y - start.points[0].point.y;
    if (Math.hypot(deltaX, deltaY) > DRAG_THRESHOLD) suppressClick.current = true;
    if (swipe.current && navigation) {
      if (!swipe.current.axis && Math.hypot(deltaX, deltaY) > DRAG_THRESHOLD) {
        swipe.current.axis = Math.abs(deltaX) > Math.abs(deltaY) * 1.25 ? "horizontal" : "vertical";
      }
      if (swipe.current.axis === "horizontal") {
        const atEdge = deltaX > 0 ? navigation.index === 0 : navigation.index === navigation.count - 1;
        scheduleTransform({ scale: 1, x: deltaX * (atEdge ? 0.2 : 1), y: 0 });
      }
    }
    if (start.transform.scale > 1) {
      scheduleTransform(panImageTransform(
        start.transform,
        deltaX,
        deltaY,
        image,
        viewport,
      ));
    }
  };

  const finishPointer = (event: ReactPointerEvent<HTMLDivElement>, cancelled: boolean) => {
    if (!pointers.current.has(event.pointerId)) return;
    const swipeStart = swipe.current;
    const direction = !cancelled && swipeStart?.axis === "horizontal"
      ? imageSwipeDirection(event.clientX - swipeStart.start.x, event.clientY - swipeStart.start.y,
          sizes().viewport.width, transformRef.current.scale)
      : 0;
    swipe.current = null;
    if (swipeStart) scheduleTransform({ scale: 1, x: 0, y: 0 });
    pointers.current.delete(event.pointerId);
    if (cancelled) suppressClick.current = true;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (pointers.current.size > 0) resetGestureStart();
    else {
      gestureStart.current = null;
      flushTransform();
      setInteracting(false);
    }
    if (direction) selectAdjacent(direction);
  };

  const onStageClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (suppressClick.current) {
      event.preventDefault();
      return;
    }
    const visual = visualElement();
    const bounds = visual?.getBoundingClientRect();
    const insideVisual = Boolean(bounds
      && event.clientX >= bounds.left && event.clientX <= bounds.right
      && event.clientY >= bounds.top && event.clientY <= bounds.bottom);
    if (insideVisual && sanitizedSvg !== undefined) {
      return;
    }
    requestClose();
  };

  return createPortal(<div ref={stageRef}
    className={`image-lightbox${entered ? " entered" : ""}${interacting ? " interacting" : ""}${navigation && navigation.count > 1 ? " gallery" : ""}`}
    role="dialog" aria-modal="true" aria-label={dialogLabel} data-lock-horizontal-swipe
    onPointerDown={onPointerDown} onPointerMove={onPointerMove}
    onPointerUp={(event) => finishPointer(event, false)}
    onPointerCancel={(event) => finishPointer(event, true)}
    onLostPointerCapture={(event) => finishPointer(event, true)}
    onClick={onStageClick}>
    <div className="image-lightbox-content">
      {sanitizedSvg !== undefined
        ? <div ref={vectorRef}
            className="image-lightbox-visual image-lightbox-vector"
            role="img" aria-label={alt}
            dangerouslySetInnerHTML={{ __html: sanitizedSvg }} />
        : src ? <img key={imageKey} ref={imageRef}
            className="image-lightbox-visual image-lightbox-image"
            src={src} alt={alt} draggable={false}
            onDragStart={(event) => event.preventDefault()} /> : null}
    </div>
    {!src && sanitizedSvg === undefined && <div className="image-lightbox-placeholder"
      onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}>
      {placeholder}
    </div>}
    {navigation && navigation.count > 1 && <div className="image-lightbox-navigation"
      onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}>
      <button type="button" aria-label="上一张图片" disabled={navigation.index === 0}
        onClick={() => selectAdjacent(-1)}><Icon name="back" size={20} /></button>
      <span role="status" aria-live="polite" aria-atomic="true">{navigation.index + 1} / {navigation.count}</span>
      <button type="button" aria-label="下一张图片" disabled={navigation.index === navigation.count - 1}
        onClick={() => selectAdjacent(1)}><Icon name="back" size={20} /></button>
    </div>}
    <button ref={closeRef} type="button" className="image-lightbox-close"
      aria-label={closeLabel}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => { event.stopPropagation(); requestClose(); }}>
      <Icon name="close" size={22} />
    </button>
  </div>, document.body);
}
