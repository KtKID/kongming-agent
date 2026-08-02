import { describe, it, expect, beforeEach } from "vitest";
import {
  selectSorted,
  useApprovalInboxStore,
} from "../useApprovalInbox";
import type {
  ApprovalInboxAddFrame,
  ApprovalInboxItem,
} from "@/protocol";

/**
 * store + selectSorted 集成 smoke：覆盖 add / remove / snapshot 全量替换路径
 * 与 selectSorted 的衔接。不渲染 React 组件（Card / Queue 已各自有用例）。
 */

function makeAddFrame(
  requestId: string,
  overrides: Partial<ApprovalInboxItem> = {},
): ApprovalInboxAddFrame {
  return {
    frame_type: "approval.inbox.add",
    requestId,
    threadId: `t-${requestId}`,
    toolName: "Bash",
    toolInput: {},
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: false,
    channel: "claude_code",
    cwd: "/p",
    arrivedAtMs: 0,
    timeoutMs: null,
    rememberRule: null,
    ...overrides,
  };
}

beforeEach(() => {
  useApprovalInboxStore.getState().clear();
});

describe("inbox + selectSorted 集成", () => {
  it("3 帧 add → selectSorted 返回 3 项（按 arrivedAtMs 递增）", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame("a", { arrivedAtMs: 30 }));
    store.applyAddFrame(makeAddFrame("b", { arrivedAtMs: 10 }));
    store.applyAddFrame(makeAddFrame("c", { arrivedAtMs: 20 }));
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.map((i) => i.requestId)).toEqual(["b", "c", "a"]);
  });

  it("注入 remove → selectSorted 减 1", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame("a"));
    store.applyAddFrame(makeAddFrame("b"));
    store.applyAddFrame(makeAddFrame("c"));
    expect(selectSorted(useApprovalInboxStore.getState()).length).toBe(3);
    store.applyRemoveFrame({
      frame_type: "approval.inbox.remove",
      requestId: "b",
      reason: "user_decided",
    });
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.length).toBe(2);
    expect(out.map((i) => i.requestId).sort()).toEqual(["a", "c"]);
  });

  it("applySnapshot 全量替换 → selectSorted 看到新列表", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame("old1"));
    store.applyAddFrame(makeAddFrame("old2"));
    store.applySnapshotFrame({
      frame_type: "approval.inbox.snapshot",
      items: [
        { ...makeAddFrame("new1") } as unknown as ApprovalInboxItem,
        { ...makeAddFrame("new2") } as unknown as ApprovalInboxItem,
      ],
    });
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.map((i) => i.requestId).sort()).toEqual(["new1", "new2"]);
  });

  it("danger 帧 add 后立即置顶", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame("safe", { arrivedAtMs: 10 }));
    store.applyAddFrame(makeAddFrame("safe2", { arrivedAtMs: 5 }));
    store.applyAddFrame(
      makeAddFrame("danger", {
        danger: true,
        blockedByRule: "rm-rf",
        arrivedAtMs: 100,
      }),
    );
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out[0].requestId).toBe("danger");
  });
});
