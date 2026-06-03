/**
 * network/tools.ts 单元测试（network-layer v0.1 步骤 5.x）
 */

import { describe, it, expect } from "vitest";
import {
  buildPingFrame,
  isHeartbeatFrame,
  exponentialBackoff,
  makeConnectionId,
} from "@/network/tools";

describe("tools.buildPingFrame", () => {
  it("buildPingFrame_returns_correct_schema", () => {
    const f = buildPingFrame(1234567890);
    expect(f).toEqual({ frame_type: "ping", ts: 1234567890 });
  });
});

describe("tools.isHeartbeatFrame", () => {
  it("isHeartbeatFrame_narrows_correctly", () => {
    expect(isHeartbeatFrame({ frame_type: "ping", ts: 1 })).toBe(true);
    expect(isHeartbeatFrame({ frame_type: "pong", ts: 1 })).toBe(true);
    expect(isHeartbeatFrame({ frame_type: "pong" })).toBe(true); // ts 可选
    expect(isHeartbeatFrame({ frame_type: "text", content: "x" })).toBe(false);
    expect(isHeartbeatFrame({ frame_type: "claude-command" })).toBe(false);
    expect(isHeartbeatFrame(null)).toBe(false);
    expect(isHeartbeatFrame(undefined)).toBe(false);
    expect(isHeartbeatFrame("string")).toBe(false);
    expect(isHeartbeatFrame(42)).toBe(false);
    expect(isHeartbeatFrame({})).toBe(false);
  });
});

describe("tools.exponentialBackoff", () => {
  it("exponentialBackoff_caps_at_30000", () => {
    expect(exponentialBackoff(0)).toBe(1_000);
    expect(exponentialBackoff(1)).toBe(2_000);
    expect(exponentialBackoff(2)).toBe(4_000);
    expect(exponentialBackoff(3)).toBe(8_000);
    expect(exponentialBackoff(4)).toBe(16_000);
    expect(exponentialBackoff(5)).toBe(30_000); // 32_000 capped
    expect(exponentialBackoff(10)).toBe(30_000);
    expect(exponentialBackoff(100)).toBe(30_000);
  });

  it("exponentialBackoff_handles_negative_input", () => {
    // 防御性：负数当 0 处理
    expect(exponentialBackoff(-1)).toBe(1_000);
    expect(exponentialBackoff(-100)).toBe(1_000);
  });
});

describe("tools.makeConnectionId", () => {
  it("makeConnectionId_returns_8_hex_chars", () => {
    const id = makeConnectionId();
    expect(id).toMatch(/^[0-9a-f]{8}$/);
  });

  it("makeConnectionId_is_unique_across_calls", () => {
    const ids = new Set<string>();
    for (let i = 0; i < 100; i++) {
      ids.add(makeConnectionId());
    }
    // 100 个 8-hex 不一定全唯一，但碰撞概率极低；至少 90+ 不重复才算正常
    expect(ids.size).toBeGreaterThanOrEqual(95);
  });
});
