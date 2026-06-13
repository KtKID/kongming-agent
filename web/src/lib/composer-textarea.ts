export const COMPOSER_TEXTAREA_MIN_ROWS = 2;
export const COMPOSER_TEXTAREA_MAX_ROWS = 6;
export const COMPOSER_TEXTAREA_FALLBACK_LINE_HEIGHT = 24;

function readPx(value: string | undefined, fallback = 0): number {
  const parsed = Number.parseFloat(value ?? "");
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function getComposerTextareaHeightBounds(
  style: Pick<
    CSSStyleDeclaration,
    "lineHeight" | "paddingTop" | "paddingBottom" | "borderTopWidth" | "borderBottomWidth"
  >,
): { minHeight: number; maxHeight: number } {
  const lineHeight = readPx(
    style.lineHeight,
    COMPOSER_TEXTAREA_FALLBACK_LINE_HEIGHT,
  );
  const verticalChrome =
    readPx(style.paddingTop) +
    readPx(style.paddingBottom) +
    readPx(style.borderTopWidth) +
    readPx(style.borderBottomWidth);

  return {
    minHeight: lineHeight * COMPOSER_TEXTAREA_MIN_ROWS + verticalChrome,
    maxHeight: lineHeight * COMPOSER_TEXTAREA_MAX_ROWS + verticalChrome,
  };
}

export function resizeComposerTextarea(el: HTMLTextAreaElement): void {
  const { minHeight, maxHeight } = getComposerTextareaHeightBounds(
    window.getComputedStyle(el),
  );
  el.style.height = "auto";
  const nextHeight = Math.min(Math.max(el.scrollHeight, minHeight), maxHeight);
  el.style.height = `${nextHeight}px`;
  el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
}
