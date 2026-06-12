import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { CountdownBar } from "../CountdownBar";

afterEach(() => {
  vi.useRealTimers();
});

describe("CountdownBar", () => {
  it("挂载时显示满进度条 + 秒数", () => {
    const now = 1_000_000;
    render(
      <CountdownBar
        deadlineMs={now + 10_000}
        onComplete={vi.fn()}
        nowProvider={() => now}
      />,
    );
    // 秒数 = 10s
    expect(screen.getByTestId("countdown-seconds").textContent).toContain("10s");
    // 进度条满
    const progress = screen.getByTestId("countdown-progress");
    expect(progress.style.width).toBe("100%");
  });

  it("倒计时到点回调 onComplete", () => {
    vi.useFakeTimers();
    const onComplete = vi.fn();
    let mockNow = 1_000_000;

    render(
      <CountdownBar
        deadlineMs={mockNow + 200}
        onComplete={onComplete}
        nowProvider={() => mockNow}
      />,
    );

    // 起初不触发
    expect(onComplete).not.toHaveBeenCalled();

    // 前进时间到截止点之后；tick 100ms 间隔走 3 次保险
    act(() => {
      mockNow += 250;
      vi.advanceTimersByTime(300);
    });

    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("onComplete 只调用一次（即使继续 tick）", () => {
    vi.useFakeTimers();
    const onComplete = vi.fn();
    let mockNow = 1_000_000;

    render(
      <CountdownBar
        deadlineMs={mockNow + 200}
        onComplete={onComplete}
        nowProvider={() => mockNow}
      />,
    );

    act(() => {
      mockNow += 300;
      vi.advanceTimersByTime(300);
    });
    act(() => {
      mockNow += 200;
      vi.advanceTimersByTime(200);
    });

    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("挂载时 deadline 已过期 → 立即触发 onComplete", () => {
    const onComplete = vi.fn();
    const now = 1_000_000;
    render(
      <CountdownBar
        deadlineMs={now - 5_000} // 过期 5 秒
        onComplete={onComplete}
        nowProvider={() => now}
      />,
    );
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("showSeconds=false 时不显示秒数文本", () => {
    const now = 1_000_000;
    render(
      <CountdownBar
        deadlineMs={now + 10_000}
        onComplete={vi.fn()}
        nowProvider={() => now}
        showSeconds={false}
      />,
    );
    expect(screen.queryByTestId("countdown-seconds")).toBeNull();
  });

  // v1.0.1: mode prop
  describe("v1.0.1 mode prop", () => {
    it("mode=approve（默认）显示'自动通过'文案 + 进度条用 primary 色", () => {
      const now = 1_000_000;
      render(
        <CountdownBar
          deadlineMs={now + 10_000}
          onComplete={vi.fn()}
          nowProvider={() => now}
        />,
      );
      const bar = screen.getByTestId("countdown-bar");
      expect(bar.getAttribute("data-mode")).toBe("approve");
      expect(screen.getByTestId("countdown-seconds").textContent).toContain("自动通过");
      const progress = screen.getByTestId("countdown-progress");
      expect(progress.className).toContain("bg-primary");
    });

    it("mode=reject 显示'自动拒绝'文案 + 进度条用 destructive 红色", () => {
      const now = 1_000_000;
      render(
        <CountdownBar
          deadlineMs={now + 10_000}
          onComplete={vi.fn()}
          mode="reject"
          nowProvider={() => now}
        />,
      );
      const bar = screen.getByTestId("countdown-bar");
      expect(bar.getAttribute("data-mode")).toBe("reject");
      expect(screen.getByTestId("countdown-seconds").textContent).toContain("自动拒绝");
      const progress = screen.getByTestId("countdown-progress");
      expect(progress.className).toContain("bg-destructive");
    });
  });
});
