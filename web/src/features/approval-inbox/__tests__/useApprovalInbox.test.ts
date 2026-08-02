import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  selectSorted,
  useApprovalInboxStore,
} from "../useApprovalInbox";
import { setSender, resetSender } from "../senderRef";
import type {
  ApprovalInboxAddFrame,
  ApprovalInboxItem,
  ApprovalInboxRemoveFrame,
  ApprovalInboxResolveFrame,
  ApprovalInboxSnapshotFrame,
} from "@/protocol";

/**
 * smart-approval-v2-inbox store 单测：reducer / selector / sender + 入口 guard。
 *
 * 设计原则验证（与 useApprovalInbox.ts 头注释对齐）：
 * - byRequestId 索引：add 后 O(1) 读
 * - resolve 不抛：sender = null / sender 抛 都不应传播
 * - hardening guard：非法 requestId / items 不污染 state
 */

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req-1",
    threadId: "thread-1",
    toolName: "Bash",
    toolInput: { cmd: "ls" },
    blockedByRule: null,
    isElevated: false,
    danger: false,
    rememberAllowed: false,
    channel: "claude_code",
    cwd: "/proj/x",
    arrivedAtMs: 1_000_000,
    timeoutMs: null,
    rememberRule: null,
    ...overrides,
  };
}

function makeAddFrame(
  overrides: Partial<ApprovalInboxItem> = {},
): ApprovalInboxAddFrame {
  return { frame_type: "approval.inbox.add", ...makeItem(overrides) };
}

function makeRemoveFrame(
  requestId: string,
  reason: ApprovalInboxRemoveFrame["reason"] = "user_decided",
): ApprovalInboxRemoveFrame {
  return { frame_type: "approval.inbox.remove", requestId, reason };
}

function makeSnapshotFrame(items: ApprovalInboxItem[]): ApprovalInboxSnapshotFrame {
  return { frame_type: "approval.inbox.snapshot", items };
}

beforeEach(() => {
  // 跨用例隔离：清 byRequestId + 解绑 sender
  useApprovalInboxStore.getState().clear();
  resetSender();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// =============================================================
// applyAddFrame
// =============================================================
describe("applyAddFrame", () => {
  it("新增 item 写入 byRequestId", () => {
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r1", toolName: "ReadFile" }));
    const got = useApprovalInboxStore.getState().byRequestId["r1"];
    expect(got).toBeDefined();
    expect(got.toolName).toBe("ReadFile");
    // 不应保留 frame_type 字段
    expect((got as unknown as { frame_type?: string }).frame_type).toBeUndefined();
  });

  it("重复 requestId → 覆盖旧 item", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "r1", toolName: "v1" }));
    store.applyAddFrame(makeAddFrame({ requestId: "r1", toolName: "v2" }));
    const got = useApprovalInboxStore.getState().byRequestId["r1"];
    expect(got.toolName).toBe("v2");
    // 仍只有 1 个 entry
    expect(Object.keys(useApprovalInboxStore.getState().byRequestId)).toEqual(["r1"]);
  });

  // ---- hardening guards ----
  it("requestId=undefined → 不写入 + warn", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore.getState().applyAddFrame({
      frame_type: "approval.inbox.add",
      // @ts-expect-error 测试入口 guard：requestId 缺失
      requestId: undefined,
    });
    expect(useApprovalInboxStore.getState().byRequestId).toEqual({});
    expect(warn).toHaveBeenCalled();
  });

  it("requestId='' → 不写入", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "" }));
    expect(useApprovalInboxStore.getState().byRequestId).toEqual({});
  });

  it("requestId=123（非 string）→ 不写入", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore.getState().applyAddFrame({
      frame_type: "approval.inbox.add",
      // @ts-expect-error 测试入口 guard：非 string requestId
      requestId: 123,
    });
    expect(useApprovalInboxStore.getState().byRequestId).toEqual({});
  });

  it("threadId='' → 不写入", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r1", threadId: "" }));
    expect(useApprovalInboxStore.getState().byRequestId).toEqual({});
  });
});

// =============================================================
// applyRemoveFrame
// =============================================================
describe("applyRemoveFrame", () => {
  it("移除已存在 item", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "r1" }));
    store.applyAddFrame(makeAddFrame({ requestId: "r2" }));
    store.applyRemoveFrame(makeRemoveFrame("r1"));
    expect(useApprovalInboxStore.getState().byRequestId["r1"]).toBeUndefined();
    expect(useApprovalInboxStore.getState().byRequestId["r2"]).toBeDefined();
  });

  it("不存在的 requestId → early return + state 引用不变", () => {
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r1" }));
    const before = useApprovalInboxStore.getState().byRequestId;
    useApprovalInboxStore.getState().applyRemoveFrame(makeRemoveFrame("never"));
    const after = useApprovalInboxStore.getState().byRequestId;
    expect(after).toBe(before); // 同一引用
  });

  // ---- hardening guard ----
  it("requestId=undefined → early return 不抛", () => {
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r1" }));
    expect(() =>
      useApprovalInboxStore.getState().applyRemoveFrame({
        frame_type: "approval.inbox.remove",
        // @ts-expect-error 测试入口 guard
        requestId: undefined,
        reason: "user_decided",
      }),
    ).not.toThrow();
    // state 不应被破坏
    expect(useApprovalInboxStore.getState().byRequestId["r1"]).toBeDefined();
  });
});

// =============================================================
// applySnapshotFrame
// =============================================================
describe("applySnapshotFrame", () => {
  it("全量替换 byRequestId", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "old1" }));
    store.applyAddFrame(makeAddFrame({ requestId: "old2" }));
    store.applySnapshotFrame(
      makeSnapshotFrame([makeItem({ requestId: "new1" })]),
    );
    const keys = Object.keys(useApprovalInboxStore.getState().byRequestId);
    expect(keys).toEqual(["new1"]);
  });

  // ---- hardening guards ----
  it("items=null → 不抛 + 不污染", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "keep" }));
    expect(() =>
      useApprovalInboxStore.getState().applySnapshotFrame({
        frame_type: "approval.inbox.snapshot",
        // @ts-expect-error 测试入口 guard：items 非数组
        items: null,
      }),
    ).not.toThrow();
    // state 不应被替换为空（reduce 早返回）
    expect(useApprovalInboxStore.getState().byRequestId["keep"]).toBeDefined();
  });

  it("items 内单条 requestId=undefined → 跳过该 item", () => {
    useApprovalInboxStore.getState().applySnapshotFrame({
      frame_type: "approval.inbox.snapshot",
      items: [
        // @ts-expect-error 测试入口 guard：item 缺 requestId
        { ...makeItem(), requestId: undefined },
        makeItem({ requestId: "valid" }),
      ],
    });
    const map = useApprovalInboxStore.getState().byRequestId;
    expect(Object.keys(map)).toEqual(["valid"]);
  });

  it("items 内单条 threadId='' → 跳过该 item", () => {
    useApprovalInboxStore.getState().applySnapshotFrame({
      frame_type: "approval.inbox.snapshot",
      items: [
        makeItem({ requestId: "invalid", threadId: "" }),
        makeItem({ requestId: "valid", threadId: "thread-valid" }),
      ],
    });
    const map = useApprovalInboxStore.getState().byRequestId;
    expect(Object.keys(map)).toEqual(["valid"]);
  });
});

// =============================================================
// setSender + resolve
// =============================================================
describe("setSender + resolve", () => {
  it("setSender 后 resolve 调 sender，参数对齐 ApprovalInboxResolveFrame", () => {
    const send = vi.fn();
    setSender(send);
    useApprovalInboxStore
      .getState()
      .resolve("thread-1", "req-1", true, {
        remember: true,
      });
    expect(send).toHaveBeenCalledTimes(1);
    const frame = send.mock.calls[0][0] as ApprovalInboxResolveFrame;
    expect(frame.frame_type).toBe("approval.inbox.resolve");
    expect(frame.threadId).toBe("thread-1");
    expect(frame.requestId).toBe("req-1");
    expect(frame.allow).toBe(true);
    expect(frame.remember).toBe(true);
    expect(frame.message).toBeUndefined();
  });

  it("resolve 发送成功后保留 item 到 authoritative remove", () => {
    const send = vi.fn();
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "req-1" }));
    setSender(send);

    store.resolve("thread-1", "req-1", true);

    expect(send).toHaveBeenCalledTimes(1);
    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
    expect(
      useApprovalInboxStore.getState().submissionByRequestId["req-1"],
    ).toEqual({ status: "submitting" });
  });

  it("sender 返回 false 时保留 item", () => {
    const send = vi.fn(() => false);
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "req-1" }));
    setSender(send);

    store.resolve("thread-1", "req-1", true);

    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
  });

  it("resolve threadId='' 时不发送 frame 且保留 item", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const send = vi.fn();
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "req-1" }));
    setSender(send);

    store.resolve("", "req-1", true);

    expect(send).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
  });

  it("resolve requestId='' 时不发送 frame", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const send = vi.fn();
    setSender(send);

    useApprovalInboxStore.getState().resolve("thread-1", "", true);

    expect(send).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
  });

  it("snapshot 保留 pending item 并清除旧 submitting", () => {
    const send = vi.fn();
    const item = makeItem({ requestId: "req-1" });
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame({ frame_type: "approval.inbox.add", ...item });
    setSender(send);

    store.resolve("thread-1", "req-1", true);
    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
    expect(
      useApprovalInboxStore.getState().submissionByRequestId["req-1"],
    ).toEqual({ status: "submitting" });

    useApprovalInboxStore
      .getState()
      .applySnapshotFrame(makeSnapshotFrame([item]));

    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
    expect(
      useApprovalInboxStore.getState().submissionByRequestId["req-1"],
    ).toBeUndefined();
  });

  it("resolve 不传 opts → frame 固定 remember=false 且不带 message", () => {
    const send = vi.fn();
    setSender(send);
    useApprovalInboxStore.getState().resolve("t", "r", false);
    const frame = send.mock.calls[0][0] as ApprovalInboxResolveFrame;
    expect(frame.allow).toBe(false);
    expect(frame.remember).toBe(false);
    expect("message" in frame).toBe(false);
  });

  it("resolve 传 remember=true → frame 只携带记忆布尔值", () => {
    const send = vi.fn();
    setSender(send);
    useApprovalInboxStore
      .getState()
      .resolve("thread-1", "req-1", true, {
        remember: true,
      });
    const frame = send.mock.calls[0][0] as ApprovalInboxResolveFrame;
    expect(frame.remember).toBe(true);
  });

  it("remember resolve 等待服务端结果并记录失败语义", () => {
    const send = vi.fn(() => true);
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "req-1" }));
    setSender(send);

    store.resolve("thread-1", "req-1", true, {
      remember: true,
    });

    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeDefined();
    useApprovalInboxStore.getState().applyResolveResultFrame({
      frame_type: "approval.inbox.resolve_result",
      requestId: "req-1",
      accepted: false,
      message: "规则保存失败，请重试",
    });
    expect(
      useApprovalInboxStore.getState().resolveResultByRequestId["req-1"],
    ).toMatchObject({ accepted: false, message: "规则保存失败，请重试" });
    expect(
      useApprovalInboxStore.getState().submissionByRequestId["req-1"],
    ).toEqual({ status: "error", message: "规则保存失败，请重试" });
  });

  it("authoritative remove 后忽略 late resolve_result", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "req-1" }));
    store.applyRemoveFrame(makeRemoveFrame("req-1"));
    store.applyResolveResultFrame({
      frame_type: "approval.inbox.resolve_result",
      requestId: "req-1",
      accepted: false,
      message: "late conflict",
    });

    expect(useApprovalInboxStore.getState().byRequestId["req-1"]).toBeUndefined();
    expect(
      useApprovalInboxStore.getState().submissionByRequestId["req-1"],
    ).toBeUndefined();
  });

  it("sender 为空时 resolve → console.warn 不抛", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r" }));
    resetSender();
    expect(() =>
      useApprovalInboxStore.getState().resolve("t", "r", true),
    ).not.toThrow();
    expect(warn).toHaveBeenCalled();
    expect(useApprovalInboxStore.getState().byRequestId["r"]).toBeDefined();
  });

  it("sender 抛 → resolve try/catch + warn 不抛", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    useApprovalInboxStore
      .getState()
      .applyAddFrame(makeAddFrame({ requestId: "r" }));
    setSender(() => {
      throw new Error("ws closed");
    });
    expect(() =>
      useApprovalInboxStore.getState().resolve("t", "r", true),
    ).not.toThrow();
    expect(warn).toHaveBeenCalled();
    expect(useApprovalInboxStore.getState().byRequestId["r"]).toBeDefined();
  });
});

// =============================================================
// clear
// =============================================================
describe("clear", () => {
  it("byRequestId 置空", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(makeAddFrame({ requestId: "a" }));
    store.applyAddFrame(makeAddFrame({ requestId: "b" }));
    store.clear();
    expect(useApprovalInboxStore.getState().byRequestId).toEqual({});
  });
});

// =============================================================
// selectSorted smoke（与 selectSorted.test.ts 互补，最小烟测）
// =============================================================
describe("selectSorted smoke", () => {
  it("空 store → []", () => {
    expect(selectSorted(useApprovalInboxStore.getState())).toEqual([]);
  });

  it("混合：danger 在前", () => {
    const store = useApprovalInboxStore.getState();
    store.applyAddFrame(
      makeAddFrame({ requestId: "safe", danger: false, blockedByRule: null, arrivedAtMs: 1 }),
    );
    store.applyAddFrame(
      makeAddFrame({
        requestId: "danger",
        danger: true,
        blockedByRule: "rm-rf",
        arrivedAtMs: 2,
      }),
    );
    const sorted = selectSorted(useApprovalInboxStore.getState());
    expect(sorted.map((i) => i.requestId)).toEqual(["danger", "safe"]);
  });
});
