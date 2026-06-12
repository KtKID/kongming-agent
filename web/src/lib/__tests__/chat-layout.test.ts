import { describe, expect, it } from "vitest";
import {
  COMPACT_BREAKPOINT,
  MOBILE_BREAKPOINT,
  WHITEBOARD_AUTO_EXPAND_BREAKPOINT,
  getChatLayoutState,
} from "@/lib/chat-layout";

describe("getChatLayoutState", () => {
  it("collapses whiteboard on mobile layout", () => {
    expect(getChatLayoutState(MOBILE_BREAKPOINT - 1)).toEqual({
      isMobileLayout: true,
      isCompactLayout: true,
      shouldOpenWhiteboard: false,
    });
  });

  it("keeps whiteboard collapsed on normal desktop widths", () => {
    expect(getChatLayoutState(COMPACT_BREAKPOINT + 120)).toEqual({
      isMobileLayout: false,
      isCompactLayout: false,
      shouldOpenWhiteboard: false,
    });
  });

  it("auto-expands whiteboard only on extra-wide desktop widths", () => {
    expect(getChatLayoutState(WHITEBOARD_AUTO_EXPAND_BREAKPOINT)).toEqual({
      isMobileLayout: false,
      isCompactLayout: false,
      shouldOpenWhiteboard: true,
    });
  });
});
