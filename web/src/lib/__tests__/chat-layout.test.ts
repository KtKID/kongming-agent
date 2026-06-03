import { describe, expect, it } from "vitest";
import {
  COMPACT_BREAKPOINT,
  MOBILE_BREAKPOINT,
  getChatLayoutState,
} from "@/lib/chat-layout";

describe("getChatLayoutState", () => {
  it("在移动端宽度下关闭白板并启用 compact", () => {
    expect(getChatLayoutState(MOBILE_BREAKPOINT - 1)).toEqual({
      isMobileLayout: true,
      isCompactLayout: true,
      shouldOpenWhiteboard: false,
    });
  });

  it("在普通桌面宽度下保留主布局并默认展开白板", () => {
    expect(getChatLayoutState(COMPACT_BREAKPOINT + 120)).toEqual({
      isMobileLayout: false,
      isCompactLayout: false,
      shouldOpenWhiteboard: true,
    });
  });
});
