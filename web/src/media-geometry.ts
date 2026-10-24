/** Reserve the same responsive rectangle before and after image decoding. */
export function messageImageGeometry(width?: number, height?: number) {
  const valid = width != null && height != null
    && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0;
  const w = valid ? width : 320;
  const h = valid ? height : 240;
  const ratio = w / h;
  return {
    width: `min(100%, ${Math.min(w, 680, 680 * ratio)}px, ${62 * ratio}vh)`,
    aspectRatio: `${w} / ${h}`,
  };
}
