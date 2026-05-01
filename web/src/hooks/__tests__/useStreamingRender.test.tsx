import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { useStreamingRender } from "@/hooks/useStreamingRender";
import {
  __setRafForTest,
  clearBuffers,
  useChatStore,
} from "@/stores/chat";
import { useApprovalDialogStore } from "@/hooks/useApprovalDialog";
import type { ThreadSocket, SocketState } from "@/lib/ws";
import type { WSFrameS2C } from "@/protocol";

/**
 * 模拟 ThreadSocket：on() 注册 listener，emit() 主动推帧。
 */
class StubSocket {
  threadId: string;
  private listeners = new Set<(f: WSFrameS2C) => void>();
  private stateListeners = new Set<(s: SocketState) => void>();
  constructor(id: string) {
    this.threadId = id;
  }
  on(fn: (f: WSFrameS2C) => void) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
  onState(fn: (s: SocketState) => void) {
    this.stateListeners.add(fn);
    return () => this.stateListeners.delete(fn);
  }
  emit(f: WSFrameS2C) {
    for (const l of this.listeners) l(f);
  }
  send = vi.fn();
  close = vi.fn();
  connect = vi.fn();
  getState = (): SocketState => "open";
}

beforeEach(() => {
  useChatStore.setState({ itemsByThread: {} });
  useApprovalDialogStore.setState({ pending: [] });
  clearBuffers();
  __setRafForTest((cb) => cb());
});

describe("useStreamingRender", () => {
  it("1000 帧 < 1000ms（同步 rAF）+ turn.end flush 后 streaming=false", () => {
    const sock = new StubSocket("t1");
    renderHook(() =>
      useStreamingRender("t1", sock as unknown as ThreadSocket),
    );
    const start = performance.now();
    act(() => {
      for (let i = 0; i < 1000; i++) {
        sock.emit({
          kind: "content.delta",
          timestamp_ms: 1,
          delta: "x",
          turn: 1,
          seq: i,
        });
      }
      sock.emit({ kind: "turn.end", timestamp_ms: 2, turn: 1 });
    });
    const dt = performance.now() - start;
    expect(dt).toBeLessThan(1000);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const a = items[0]!;
    if (a.kind !== "assistant") throw new Error("expected assistant");
    expect(a.content.length).toBe(1000);
    expect(a.streaming).toBe(false);
  });

  it("approval.request → 推 useApprovalDialog 队列 + chat store appendApprovalRequest", () => {
    const sock = new StubSocket("t1");
    renderHook(() =>
      useStreamingRender("t1", sock as unknown as ThreadSocket),
    );
    act(() => {
      sock.emit({
        kind: "approval.request",
        timestamp_ms: 1,
        call_id: "c1",
        tool_name: "Shell",
        arguments: { cmd: "ls" },
        turn: 1,
      });
    });
    expect(useApprovalDialogStore.getState().pending.length).toBe(1);
    const items = useChatStore.getState().itemsByThread["t1"]!;
    expect(items.find((it) => it.kind === "approval")).toBeDefined();
  });

  it("error 帧 → appendError 横幅项", () => {
    const sock = new StubSocket("t1");
    renderHook(() =>
      useStreamingRender("t1", sock as unknown as ThreadSocket),
    );
    act(() => {
      sock.emit({
        kind: "error",
        timestamp_ms: 1,
        error_code: "internal",
        message: "boom",
      });
    });
    const items = useChatStore.getState().itemsByThread["t1"]!;
    const err = items.find((it) => it.kind === "error");
    expect(err).toBeDefined();
  });
});
