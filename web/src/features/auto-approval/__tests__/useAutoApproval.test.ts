import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  queryAutoApproval,
  selectCwdState,
  toggleAutoApproval,
  useAutoApprovalStore,
  type AutoApprovalSocket,
} from "../useAutoApproval";
import type { AutoApprovalStateFrame } from "../types";

function makeStateFrame(overrides: Partial<AutoApprovalStateFrame> = {}): AutoApprovalStateFrame {
  return {
    frame_type: "auto_approval_state",
    channel: "claude_code",
    cwd: "/proj/x",
    enabled: false,
    timeoutMs: 10000,
    ruleOverrides: {},
    ...overrides,
  };
}

beforeEach(() => {
  useAutoApprovalStore.getState().clear();
});

describe("useAutoApprovalStore.applyStateFrame", () => {
  it("写入新 cwd 状态", () => {
    useAutoApprovalStore.getState().applyStateFrame(
      makeStateFrame({ cwd: "/p1", enabled: true, timeoutMs: 5000 }),
    );
    const got = useAutoApprovalStore.getState().byCwd["/p1"];
    expect(got).toEqual({ enabled: true, timeoutMs: 5000, ruleOverrides: {} });
  });

  it("覆盖同 cwd 旧状态", () => {
    const store = useAutoApprovalStore.getState();
    store.applyStateFrame(makeStateFrame({ cwd: "/p", enabled: true }));
    store.applyStateFrame(makeStateFrame({ cwd: "/p", enabled: false }));
    expect(useAutoApprovalStore.getState().byCwd["/p"].enabled).toBe(false);
  });

  it("多 cwd 独立存", () => {
    const store = useAutoApprovalStore.getState();
    store.applyStateFrame(makeStateFrame({ cwd: "/a", enabled: true }));
    store.applyStateFrame(makeStateFrame({ cwd: "/b", enabled: false }));
    expect(useAutoApprovalStore.getState().byCwd["/a"].enabled).toBe(true);
    expect(useAutoApprovalStore.getState().byCwd["/b"].enabled).toBe(false);
  });

  it("ruleOverrides 被独立拷贝（不共享引用）", () => {
    const overrides = { bash_sudo: false };
    useAutoApprovalStore.getState().applyStateFrame(
      makeStateFrame({ cwd: "/p", ruleOverrides: overrides }),
    );
    overrides.bash_sudo = true; // 外部 mutate
    expect(useAutoApprovalStore.getState().byCwd["/p"].ruleOverrides).toEqual({
      bash_sudo: false,
    });
  });
});

describe("selectCwdState", () => {
  it("拿到对应 cwd 状态", () => {
    useAutoApprovalStore.getState().applyStateFrame(
      makeStateFrame({ cwd: "/x", enabled: true }),
    );
    const sel = selectCwdState("/x");
    const got = sel(useAutoApprovalStore.getState());
    expect(got?.enabled).toBe(true);
  });

  it("空 cwd 返回 null", () => {
    const sel = selectCwdState(undefined);
    expect(sel(useAutoApprovalStore.getState())).toBeNull();
  });

  it("未知 cwd 返回 null", () => {
    const sel = selectCwdState("/never");
    expect(sel(useAutoApprovalStore.getState())).toBeNull();
  });
});

describe("queryAutoApproval", () => {
  it("发 auto-approval-query 帧", () => {
    const send = vi.fn();
    const socket: AutoApprovalSocket = { send };
    queryAutoApproval(socket, "/p");
    expect(send).toHaveBeenCalledWith({ frame_type: "auto-approval-query", cwd: "/p" });
  });

  it("空 cwd 不发送", () => {
    const send = vi.fn();
    queryAutoApproval({ send }, "");
    expect(send).not.toHaveBeenCalled();
  });

  it("send 抛错不传播", () => {
    const send = vi.fn(() => {
      throw new Error("socket closed");
    });
    expect(() => queryAutoApproval({ send }, "/p")).not.toThrow();
  });
});

describe("toggleAutoApproval", () => {
  it("发 toggle 帧 + optimistic 更新 store", () => {
    const send = vi.fn();
    toggleAutoApproval({ send }, "/p", true);
    expect(send).toHaveBeenCalledWith({
      frame_type: "auto-approval-toggle",
      cwd: "/p",
      enabled: true,
    });
    expect(useAutoApprovalStore.getState().byCwd["/p"].enabled).toBe(true);
  });

  it("保留 timeoutMs / ruleOverrides 既有值（optimistic 不覆盖未知字段）", () => {
    useAutoApprovalStore.getState().applyStateFrame(
      makeStateFrame({
        cwd: "/p",
        enabled: false,
        timeoutMs: 5000,
        ruleOverrides: { bash_sudo: false },
      }),
    );
    toggleAutoApproval({ send: vi.fn() }, "/p", true);
    const s = useAutoApprovalStore.getState().byCwd["/p"];
    expect(s.enabled).toBe(true);
    expect(s.timeoutMs).toBe(5000);
    expect(s.ruleOverrides).toEqual({ bash_sudo: false });
  });

  it("send 抛错时不 optimistic 更新", () => {
    const send = vi.fn(() => {
      throw new Error("socket closed");
    });
    toggleAutoApproval({ send }, "/p", true);
    expect(useAutoApprovalStore.getState().byCwd["/p"]).toBeUndefined();
  });

  it("空 cwd 不发送", () => {
    const send = vi.fn();
    toggleAutoApproval({ send }, "", true);
    expect(send).not.toHaveBeenCalled();
  });
});
