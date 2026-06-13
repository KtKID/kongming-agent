import { describe, expect, it, vi } from "vitest";
import { bindViewportHeightVar, syncViewportHeightVar } from "@/lib/viewport";

function makeVisualViewport(height: number) {
  const listeners = new Map<string, Set<() => void>>();
  return {
    height,
    addEventListener: vi.fn((event: string, handler: () => void) => {
      const group = listeners.get(event) ?? new Set<() => void>();
      group.add(handler);
      listeners.set(event, group);
    }),
    removeEventListener: vi.fn((event: string, handler: () => void) => {
      listeners.get(event)?.delete(handler);
    }),
    emit(event: string) {
      for (const handler of listeners.get(event) ?? []) {
        handler();
      }
    },
  };
}

describe("viewport height sync", () => {
  it("prefers visualViewport height", () => {
    const visualViewport = makeVisualViewport(612);
    const win = {
      innerHeight: 900,
      visualViewport,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as Window;

    expect(syncViewportHeightVar(document, win)).toBe("612px");
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("612px");
  });

  it("falls back to innerHeight when visualViewport is unavailable", () => {
    const win = {
      innerHeight: 734,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as Window;

    expect(syncViewportHeightVar(document, win)).toBe("734px");
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("734px");
  });

  it("binds resize listeners and keeps css var in sync", () => {
    const visualViewport = makeVisualViewport(640);
    const win = {
      innerHeight: 820,
      visualViewport,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as Window;

    const unbind = bindViewportHeightVar(document, win);
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("640px");

    visualViewport.height = 588;
    visualViewport.emit("resize");
    expect(document.documentElement.style.getPropertyValue("--app-height")).toBe("588px");

    unbind();
    expect(win.removeEventListener).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(visualViewport.removeEventListener).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(visualViewport.removeEventListener).toHaveBeenCalledWith("scroll", expect.any(Function));
  });
});
