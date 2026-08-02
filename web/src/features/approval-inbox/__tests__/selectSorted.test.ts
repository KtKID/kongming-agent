import { describe, it, expect, beforeEach } from "vitest";
import {
  selectSorted,
  useApprovalInboxStore,
} from "../useApprovalInbox";
import type { ApprovalInboxItem } from "@/protocol";

/**
 * selectSorted 排序选择器单测。
 *
 * 规则（与 useApprovalInbox.ts L120-135 注释对齐）：
 * - danger 卡置顶
 * - 每组内按 arrivedAtMs 递增（旧 → 新）
 * - 同 arrivedAtMs 应保持稳定（Array.sort 不保证 stable 在老引擎；现代 V8 稳定）
 */

function makeItem(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    requestId: "req",
    threadId: "t",
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

function seed(items: ApprovalInboxItem[]) {
  const byRequestId: Record<string, ApprovalInboxItem> = {};
  for (const i of items) byRequestId[i.requestId] = i;
  useApprovalInboxStore.setState({ byRequestId });
}

describe("selectSorted", () => {
  it("空 byRequestId → []", () => {
    expect(selectSorted(useApprovalInboxStore.getState())).toEqual([]);
  });

  it("全 safe → 按 arrivedAtMs 递增", () => {
    seed([
      makeItem({ requestId: "c", arrivedAtMs: 30 }),
      makeItem({ requestId: "a", arrivedAtMs: 10 }),
      makeItem({ requestId: "b", arrivedAtMs: 20 }),
    ]);
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.map((i) => i.requestId)).toEqual(["a", "b", "c"]);
  });

  it("全 danger → 按 arrivedAtMs 递增", () => {
    seed([
      makeItem({
        requestId: "x",
        danger: true,
        blockedByRule: "rm-rf",
        arrivedAtMs: 200,
      }),
      makeItem({ requestId: "y", danger: true, blockedByRule: "sudo", arrivedAtMs: 100 }),
    ]);
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.map((i) => i.requestId)).toEqual(["y", "x"]);
  });

  it("混合：danger 全在前；组内各按 arrivedAtMs 递增", () => {
    seed([
      makeItem({ requestId: "safe-late", arrivedAtMs: 50 }),
      makeItem({
        requestId: "danger-late",
        danger: true,
        blockedByRule: "r1",
        arrivedAtMs: 40,
      }),
      makeItem({ requestId: "safe-early", arrivedAtMs: 10 }),
      makeItem({
        requestId: "danger-early",
        danger: true,
        blockedByRule: "r2",
        arrivedAtMs: 20,
      }),
    ]);
    const out = selectSorted(useApprovalInboxStore.getState());
    expect(out.map((i) => i.requestId)).toEqual([
      "danger-early",
      "danger-late",
      "safe-early",
      "safe-late",
    ]);
  });

  it("同 arrivedAtMs → 稳定（同组内不抖动）", () => {
    // seed 顺序：先 a 再 b（同 arrivedAtMs）
    seed([
      makeItem({ requestId: "a", arrivedAtMs: 100 }),
      makeItem({ requestId: "b", arrivedAtMs: 100 }),
      makeItem({ requestId: "c", arrivedAtMs: 100 }),
    ]);
    const out1 = selectSorted(useApprovalInboxStore.getState());
    const out2 = selectSorted(useApprovalInboxStore.getState());
    // 调两次 → 顺序一致
    expect(out2.map((i) => i.requestId)).toEqual(out1.map((i) => i.requestId));
    // 都来自 byRequestId.Object.values，顺序由插入顺序决定
    expect(out1.length).toBe(3);
  });
});
