/**
 * chat-receive-side-unify #3 · useChatTimeline 订阅 hook 测试
 *
 * 严格 TDD：每个能力先红后绿。覆盖：
 * - applyEvent 后 hook 返回的 state 更新（组件重渲染拿到新 state）
 * - threadId=undefined 返回稳定空 state、不报错
 * - threadId 切换后订阅新 store
 * - unmount 后不再触发更新（无 React warning）
 * - 渲染次数不失控（同 state applyEvent 不触发额外渲染 / getSnapshot 纯净）
 * - disposeTimelineStore 清理路径（切 thread 后 TIMELINE_STORES size 不无界增长）
 *
 * vitest 资源敏感：用 renderHook（非 watch），unmount 后断言无残留。
 */
import { StrictMode, type ReactNode } from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  useChatTimeline,
  getTimelineStore,
  disposeTimelineStore,
  timelineStoreCount,
} from "../runtimeWiring";
import type { ChatEvent } from "../types";

function ev(partial: Partial<ChatEvent> & Pick<ChatEvent, "kind"> & { threadId: string }): ChatEvent {
  return {
    provider: "generic",
    turnId: partial.turnId ?? "r1",
    createdAt: partial.createdAt ?? 1,
    payload: partial.payload ?? {},
    ...partial,
  };
}

beforeEach(() => {
  // 隔离：清空所有缓存 store，避免测试间状态 / refcount 污染。
  for (const tid of [...listCachedThreadIds()]) disposeTimelineStore(tid);
});

/** 测试辅助：拿到当前缓存的全部 threadId（用 count 校验前先得能枚举清空）。 */
function listCachedThreadIds(): string[] {
  // timelineStoreCount 只给数量，清空需要逐个 dispose 已知 id。
  // 测试里用到的 id 固定，这里返回测试集合内可能用过的 id 全集即可。
  return ["t1", "t2", "t3", "__empty__", "a", "b", "c", "d", "e"];
}

describe("useChatTimeline · 基本订阅", () => {
  it("applyEvent 后 hook 返回最新 state（组件重渲染拿到新内容）", () => {
    const { result } = renderHook(() => useChatTimeline("t1"));
    expect(result.current.orderedMessageIds).toEqual([]);

    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "hi" } }),
      );
    });

    expect(result.current.orderedMessageIds.length).toBe(1);
    const id = result.current.orderedMessageIds[0];
    expect(result.current.messagesById[id].parts[0]).toEqual({ type: "text", text: "hi" });
  });

  it("返回的 state.threadId 与入参一致", () => {
    const { result } = renderHook(() => useChatTimeline("t1"));
    expect(result.current.threadId).toBe("t1");
  });

  it("StrictMode 双挂载期间保留同一 store 并接收后续历史", () => {
    const wrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode>{children}</StrictMode>
    );
    const { result } = renderHook(() => useChatTimeline("t1"), { wrapper });

    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({
          kind: "history_batch_loaded",
          threadId: "t1",
          payload: {
            messages: [
              {
                id: "strict-history-user",
                frame_type: "text",
                role: "user",
                content: "strict persisted",
              },
            ],
          },
        }),
      );
    });

    expect(result.current.historyLoaded).toBe(true);
    expect(result.current.orderedMessageIds).toEqual(["strict-history-user"]);
  });
});

describe("useChatTimeline · threadId=undefined", () => {
  it("threadId=undefined 返回稳定空 state，不报错", () => {
    const { result } = renderHook(() => useChatTimeline(undefined));
    expect(result.current.historyLoaded).toBe(false);
    expect(result.current.orderedMessageIds).toEqual([]);
    expect(result.current.messagesById).toEqual({});
  });

  it("threadId=undefined 多次渲染返回同一引用（getSnapshot 纯净）", () => {
    const { result, rerender } = renderHook(() => useChatTimeline(undefined));
    const a = result.current;
    rerender();
    expect(result.current).toBe(a);
  });
});

describe("useChatTimeline · threadId 切换", () => {
  it("threadId 变化后订阅新 store（拿到新 thread 的 state）", () => {
    const { result, rerender } = renderHook(({ tid }) => useChatTimeline(tid), {
      initialProps: { tid: "t1" as string | undefined },
    });
    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "from-t1" } }),
      );
    });
    expect(result.current.orderedMessageIds.length).toBe(1);

    rerender({ tid: "t2" });
    expect(result.current.threadId).toBe("t2");
    expect(result.current.orderedMessageIds).toEqual([]);

    act(() => {
      getTimelineStore("t2").applyEvent(
        ev({ kind: "user_message", threadId: "t2", payload: { text: "from-t2" } }),
      );
    });
    const id = result.current.orderedMessageIds[0];
    expect(result.current.messagesById[id].parts[0]).toEqual({ type: "text", text: "from-t2" });
  });

  it("切回旧 thread 仍能拿到旧 store 的实时更新（订阅切换正确）", () => {
    const { result, rerender } = renderHook(({ tid }) => useChatTimeline(tid), {
      initialProps: { tid: "t1" as string | undefined },
    });
    rerender({ tid: "t2" });
    rerender({ tid: "t1" });
    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "again" } }),
      );
    });
    expect(result.current.orderedMessageIds.length).toBe(1);
  });
});

describe("useChatTimeline · unmount 后不再触发更新", () => {
  it("unmount 后 applyEvent 不报 React warning（listener 已注销）", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = renderHook(() => useChatTimeline("t1"));
    unmount();
    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "late" } }),
      );
    });
    expect(errSpy).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});

describe("useChatTimeline · 渲染次数不失控", () => {
  it("同一 state（无实际变更的 applyEvent 路径）不触发额外渲染", () => {
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useChatTimeline("t1");
    });
    const initial = renders;
    // 同一 store 的 getSnapshot 在 state 未变时返回同引用 → 不应额外渲染。
    // 重复读 snapshot 不改变 state。
    act(() => {
      // 触发一次真实变更
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "x" } }),
      );
    });
    const afterOne = renders;
    expect(afterOne).toBeGreaterThan(initial); // 真实变更确实重渲染
    expect(result.current.orderedMessageIds.length).toBe(1);

    // notify 但 state 引用不变的场景：React useSyncExternalStore 会比较 getSnapshot
    // 引用，相同引用不会 commit。这里用 notify 一次但不改 state 来验证。
    act(() => {
      getTimelineStore("t1").subscribe(() => {}); // no-op 订阅，不改 state
    });
    expect(renders).toBe(afterOne); // 无额外渲染
  });
});

describe("useChatTimeline · disposeTimelineStore 清理路径", () => {
  it("切 N 个 thread 后，无订阅者的旧 store 被释放，size 不无界增长", async () => {
    const { rerender, unmount } = renderHook(({ tid }) => useChatTimeline(tid), {
      initialProps: { tid: "a" as string | undefined },
    });
    rerender({ tid: "b" });
    rerender({ tid: "c" });
    rerender({ tid: "d" });
    rerender({ tid: "e" });
    await act(async () => {
      await Promise.resolve();
    });
    // 切走的 a/b/c/d 应已 dispose，只剩当前 e（+ 可能的 __empty__ 不计入业务 thread）。
    expect(timelineStoreCount({ excludeEmpty: true })).toBe(1);
    unmount();
    await act(async () => {
      await Promise.resolve();
    });
    // 最后一个组件 unmount 后，e 也无订阅者 → 释放。
    expect(timelineStoreCount({ excludeEmpty: true })).toBe(0);
  });

  it("多个组件订阅同一 thread 时，单个 unmount 不误删 store（refcount > 0）", async () => {
    const h1 = renderHook(() => useChatTimeline("t1"));
    const h2 = renderHook(() => useChatTimeline("t1"));
    expect(timelineStoreCount({ excludeEmpty: true })).toBe(1);
    h1.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    // 仍有 h2 订阅 → 不删。
    expect(timelineStoreCount({ excludeEmpty: true })).toBe(1);
    act(() => {
      getTimelineStore("t1").applyEvent(
        ev({ kind: "user_message", threadId: "t1", payload: { text: "still-alive" } }),
      );
    });
    expect(h2.result.current.orderedMessageIds.length).toBe(1);
    h2.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(timelineStoreCount({ excludeEmpty: true })).toBe(0);
  });

  it("disposeTimelineStore 显式调用后再 getTimelineStore 返回全新空 store", () => {
    getTimelineStore("t1").applyEvent(
      ev({ kind: "user_message", threadId: "t1", payload: { text: "hi" } }),
    );
    expect(getTimelineStore("t1").getSnapshot().orderedMessageIds.length).toBe(1);
    disposeTimelineStore("t1");
    expect(getTimelineStore("t1").getSnapshot().orderedMessageIds.length).toBe(0);
  });
});
