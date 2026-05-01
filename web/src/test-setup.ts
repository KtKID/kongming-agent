import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// 每个 test 之后自动清理 DOM，避免 component 残留
afterEach(() => {
  cleanup();
});

// jsdom 缺少 matchMedia / requestAnimationFrame 兜底
if (typeof window !== "undefined") {
  if (!window.matchMedia) {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
  }
  // jsdom 默认提供 rAF；保留这里防御 stub 缺失
  if (!window.requestAnimationFrame) {
    window.requestAnimationFrame = (cb) =>
      window.setTimeout(() => cb(performance.now()), 16) as unknown as number;
    window.cancelAnimationFrame = (id) => window.clearTimeout(id);
  }
  // jsdom 缺少 Element.scrollTo / scrollIntoView
  if (!(Element.prototype as { scrollTo?: unknown }).scrollTo) {
    Element.prototype.scrollTo = () => undefined;
  }
  if (!(Element.prototype as { scrollIntoView?: unknown }).scrollIntoView) {
    Element.prototype.scrollIntoView = () => undefined;
  }
  // jsdom 不实现 PointerEvent；某些 radix 组件依赖
  if (typeof window.PointerEvent === "undefined") {
    class FakePointerEvent extends MouseEvent {}
    (window as unknown as { PointerEvent: typeof FakePointerEvent }).PointerEvent =
      FakePointerEvent;
  }
  // hasPointerCapture / setPointerCapture / releasePointerCapture 兜底（radix select 用）
  const ep = Element.prototype as unknown as Record<string, unknown>;
  if (!ep.hasPointerCapture) ep.hasPointerCapture = () => false;
  if (!ep.setPointerCapture) ep.setPointerCapture = () => undefined;
  if (!ep.releasePointerCapture) ep.releasePointerCapture = () => undefined;

  // ResizeObserver 兜底（radix scroll-area / framer-motion 等）
  if (typeof window.ResizeObserver === "undefined") {
    class FakeResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (
      window as unknown as { ResizeObserver: typeof FakeResizeObserver }
    ).ResizeObserver = FakeResizeObserver;
    (
      globalThis as unknown as { ResizeObserver: typeof FakeResizeObserver }
    ).ResizeObserver = FakeResizeObserver;
  }
  // IntersectionObserver 兜底
  if (typeof window.IntersectionObserver === "undefined") {
    class FakeIntersectionObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    (
      window as unknown as {
        IntersectionObserver: typeof FakeIntersectionObserver;
      }
    ).IntersectionObserver = FakeIntersectionObserver;
  }
}
