/**
 * chat-running-state-unify #1：useThreadRunning + RUNNING_PHASES 锁定。
 *
 * 五种 phase 输入（DoD 要求）+ undefined + 未知 threadId 全覆盖。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { RUNNING_PHASES, useThreadRunning } from "../useThreadRunning";
import { useThreadStatusStore } from "@/stores/threadStatus";

const TID = "t1";

beforeEach(() => {
  // 隔离：清空 store statuses，避免测试间状态污染。
  useThreadStatusStore.setState({
    statuses: {},
    connectionGeneration: 1,
    lastSequence: 0,
  });
});

describe("useThreadRunning · RUNNING_PHASES 常量", () => {
  it("锁定四个运行态（responding / thinking / tool_calling / waiting_approval）", () => {
    expect([...RUNNING_PHASES].sort()).toEqual(
      ["responding", "thinking", "tool_calling", "waiting_approval"].sort(),
    );
  });

  it("idle / complete / error 不在集合内（结束态必须 false）", () => {
    expect(RUNNING_PHASES.has("idle")).toBe(false);
    expect(RUNNING_PHASES.has("complete")).toBe(false);
    expect(RUNNING_PHASES.has("error")).toBe(false);
  });
});

describe("useThreadRunning · phase 判定", () => {
  it.each(["responding", "thinking", "tool_calling", "waiting_approval"] as const)(
    "phase=%s → true",
    (phase) => {
      useThreadStatusStore.getState().applyStatus({
        frame_type: "thread-status",
        threadId: TID,
        phase,
        sequence: 1,
        runId: "run-1",
        runGeneration: 1,
      }, 1);
      const { result } = renderHook(() => useThreadRunning(TID));
      expect(result.current).toBe(true);
    },
  );

  it.each(["idle", "complete", "error"] as const)(
    "phase=%s → false（结束/空闲/错误必须复位）",
    (phase) => {
      useThreadStatusStore.getState().applyStatus({
        frame_type: "thread-status",
        threadId: TID,
        phase,
        sequence: 1,
        runId: "run-1",
        runGeneration: 1,
      }, 1);
      const { result } = renderHook(() => useThreadRunning(TID));
      expect(result.current).toBe(false);
    },
  );

  it("running phase 携带 run_end_reason 时终态兜底返回 false", () => {
    useThreadStatusStore.getState().applyStatus({
      frame_type: "thread-status",
      threadId: TID,
      phase: "responding",
      sequence: 1,
      runId: "run-1",
      runGeneration: 1,
      run_end_reason: 8,
    }, 1);
    const { result } = renderHook(() => useThreadRunning(TID));

    expect(result.current).toBe(false);
  });

  it("threadId=undefined → false（未进入会话）", () => {
    const { result } = renderHook(() => useThreadRunning(undefined));
    expect(result.current).toBe(false);
  });

  it("threadId 在 store 无 entry → false（尚未收到广播）", () => {
    const { result } = renderHook(() => useThreadRunning("unknown"));
    expect(result.current).toBe(false);
  });
});
