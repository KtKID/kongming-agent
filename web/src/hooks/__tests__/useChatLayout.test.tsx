import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MOBILE_BREAKPOINT } from "@/lib/chat-layout";
import { useChatLayout } from "@/hooks/useChatLayout";

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

describe("useChatLayout", () => {
  it("derives layout state from the shared breakpoint rules", () => {
    setViewport(1280);

    const { result } = renderHook(() => useChatLayout());

    expect(result.current.isMobileLayout).toBe(false);
    expect(result.current.isCompactLayout).toBe(false);
    expect(result.current.shouldOpenWhiteboard).toBe(false);
  });

  it("updates layout state after resize", () => {
    setViewport(1280);

    const { result } = renderHook(() => useChatLayout());

    act(() => {
      setViewport(MOBILE_BREAKPOINT - 1);
    });

    expect(result.current.isMobileLayout).toBe(true);
    expect(result.current.isCompactLayout).toBe(true);
    expect(result.current.shouldOpenWhiteboard).toBe(false);
  });
});
