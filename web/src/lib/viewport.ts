const VIEWPORT_HEIGHT_VAR = "--app-height";

function readViewportHeight(win: Window): number {
  const visualHeight = win.visualViewport?.height;
  if (typeof visualHeight === "number" && Number.isFinite(visualHeight) && visualHeight > 0) {
    return Math.round(visualHeight);
  }
  return win.innerHeight;
}

export function syncViewportHeightVar(
  doc: Document = document,
  win: Window = window,
): string {
  const nextValue = `${readViewportHeight(win)}px`;
  doc.documentElement.style.setProperty(VIEWPORT_HEIGHT_VAR, nextValue);
  return nextValue;
}

export function bindViewportHeightVar(
  doc: Document = document,
  win: Window = window,
): () => void {
  const sync = () => {
    syncViewportHeightVar(doc, win);
  };

  sync();
  win.addEventListener("resize", sync);
  win.visualViewport?.addEventListener("resize", sync);
  win.visualViewport?.addEventListener("scroll", sync);

  return () => {
    win.removeEventListener("resize", sync);
    win.visualViewport?.removeEventListener("resize", sync);
    win.visualViewport?.removeEventListener("scroll", sync);
  };
}
