import { describe, it, expect } from "vitest";
import { formatRelative } from "@/lib/relative-time";

describe("formatRelative", () => {
  const NOW = 1_700_000_000;  // 固定 now 让测试稳定

  it("<60 秒 → 刚刚", () => {
    expect(formatRelative(NOW - 30, NOW)).toBe("刚刚");
    expect(formatRelative(NOW, NOW)).toBe("刚刚");
  });

  it("<60 分钟 → X 分钟前", () => {
    expect(formatRelative(NOW - 60, NOW)).toBe("1 分钟前");
    expect(formatRelative(NOW - 600, NOW)).toBe("10 分钟前");
    expect(formatRelative(NOW - 3599, NOW)).toBe("59 分钟前");
  });

  it("<24 小时 → X 小时前", () => {
    expect(formatRelative(NOW - 3600, NOW)).toBe("1 小时前");
    expect(formatRelative(NOW - 7200, NOW)).toBe("2 小时前");
    expect(formatRelative(NOW - 86399, NOW)).toBe("23 小时前");
  });

  it(">=24 小时 → YYYY-MM-DD", () => {
    // unix 1_700_000_000 = 2023-11-14 22:13:20 UTC
    const result = formatRelative(NOW - 86400, NOW);
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("默认 now=Date.now()/1000", () => {
    // 不传 now 时，传一个非常老的时间 → 应该是 YYYY-MM-DD
    const result = formatRelative(0);
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});
