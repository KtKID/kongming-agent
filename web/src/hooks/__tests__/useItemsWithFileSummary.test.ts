import { renderHook } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import {
  useItemsWithFileSummary,
  extractFilePath,
  type FilesSummaryItem,
} from "@/hooks/useItemsWithFileSummary";

// ---------- 辅助类型 & 工厂 ----------

interface UserItem {
  kind: "user";
  id: string;
  content: string;
  threadId: string;
  timestampMs: number;
}

interface ToolItem {
  kind: "tool";
  id: string;
  threadId: string;
  turn: number;
  runId: string;
  toolName: string;
  callId: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  resultData: Record<string, unknown> | null;
  timestampMs: number;
}

interface AssistantItem {
  kind: "assistant";
  id: string;
  content: string;
  threadId: string;
  timestampMs: number;
}

type TestItem = UserItem | ToolItem | AssistantItem;

/** 创建 user 消息（作为轮次分界） */
const userItem = (id: string): UserItem => ({
  kind: "user",
  id,
  content: "hi",
  threadId: "t1",
  timestampMs: 0,
});

/** 创建 tool 消息，可自定义 arguments / resultData */
const toolItem = (
  id: string,
  toolName: string,
  ok: boolean,
  extra?: { arguments?: Record<string, unknown>; resultData?: Record<string, unknown> | null },
): ToolItem => ({
  kind: "tool",
  id,
  threadId: "t1",
  turn: 1,
  runId: "",
  toolName,
  callId: id,
  arguments: extra?.arguments ?? {},
  ok,
  resultData: extra?.resultData ?? null,
  timestampMs: 0,
});

/** 创建 assistant 消息（不触发文件提取） */
const assistantItem = (id: string): AssistantItem => ({
  kind: "assistant",
  id,
  content: "hello",
  threadId: "t1",
  timestampMs: 0,
});

/** 判断是否为 user 消息 */
const isUser = (item: TestItem) => item.kind === "user";

/** 从结果数组中取出所有 files-summary 项 */
const summaries = (result: Array<TestItem | FilesSummaryItem>): FilesSummaryItem[] =>
  result.filter((it): it is FilesSummaryItem => (it as FilesSummaryItem).kind === "files-summary");

// ---------- 测试用例 ----------

// ========== extractFilePath 直接单元测试（6 分支） ==========

describe("extractFilePath（纯函数直接测试）", () => {
  it("write_file + ok=true + resultData.path → 返回 path", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: true,
        toolName: "write_file",
        resultData: { path: "/tmp/a.py" },
      }),
    ).toBe("/tmp/a.py");
  });

  it.each(["Write", "Edit", "MultiEdit"])(
    "%s + ok=true + arguments.file_path → 返回 file_path",
    (tool) => {
      expect(
        extractFilePath({
          kind: "tool",
          ok: true,
          toolName: tool,
          arguments: { file_path: `/src/${tool}.ts` },
        }),
      ).toBe(`/src/${tool}.ts`);
    },
  );

  it("ok=false → null", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: false,
        toolName: "write_file",
        resultData: { path: "/tmp/a.py" },
      }),
    ).toBeNull();
  });

  it("ok=null → null（ok 不是严格 true）", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: null,
        toolName: "write_file",
        resultData: { path: "/tmp/a.py" },
      }),
    ).toBeNull();
  });

  it("kind 不是 tool → null", () => {
    expect(
      extractFilePath({ kind: "assistant", content: "hello" }),
    ).toBeNull();
  });

  it("write_file 无 resultData.path 字段 → null", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: true,
        toolName: "write_file",
        resultData: { message: "done" },
      }),
    ).toBeNull();
  });

  it("Write 无 arguments.file_path 字段 → null", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: true,
        toolName: "Write",
        arguments: { content: "x" },
      }),
    ).toBeNull();
  });

  it("非写类工具（ReadFile）→ null", () => {
    expect(
      extractFilePath({
        kind: "tool",
        ok: true,
        toolName: "ReadFile",
        resultData: { path: "/tmp/read.py" },
      }),
    ).toBeNull();
  });

  it("传入 null → null", () => {
    expect(extractFilePath(null)).toBeNull();
  });

  it("传入非对象（字符串）→ null", () => {
    expect(extractFilePath("not an object")).toBeNull();
  });
});

describe("useItemsWithFileSummary", () => {
  // ========== extractFilePath 间接测试 ==========

  describe("extractFilePath — write_file（resultData.path）", () => {
    it("ok=true + resultData.path 存在 → 文件被收集", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "write_file", true, {
          resultData: { path: "/tmp/a.py" },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toEqual(["/tmp/a.py"]);
    });
  });

  describe("extractFilePath — Write / Edit / MultiEdit（arguments.file_path）", () => {
    it.each(["Write", "Edit", "MultiEdit"])("%s → 从 arguments.file_path 提取", (tool) => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", tool, true, {
          arguments: { file_path: `/src/${tool.toLowerCase()}.ts` },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toEqual([`/src/${tool.toLowerCase()}.ts`]);
    });
  });

  describe("extractFilePath — 拒绝无效 item", () => {
    it("ok=false → 不提取", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "write_file", false, {
          resultData: { path: "/tmp/fail.py" },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      // 没有文件修改 → 不插入汇总
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });

    it("ok=null → 不提取（ok 不是严格 true）", () => {
      // ok 类型声明为 boolean 但运行时可能收到 null
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "write_file", null as unknown as boolean, {
          resultData: { path: "/tmp/null-ok.py" },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });

    it("缺失 resultData（write_file）→ 不提取", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "write_file", true),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });

    it("缺失 arguments（Write）→ 不提取", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "Write", true),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });

    it("kind 不是 tool → 不提取", () => {
      const items: TestItem[] = [
        userItem("u1"),
        assistantItem("a1"),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });

    it("未知 toolName → 不提取", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "ReadFile", true, {
          resultData: { path: "/tmp/read.py" },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
    });
  });

  // ========== 边界条件 ==========

  describe("边界条件", () => {
    it("空 items 数组 → 原样返回空数组", () => {
      const items: TestItem[] = [];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      expect(result.current).toEqual([]);
      // 引用相同（短路返回原数组）
      expect(result.current).toBe(items);
    });

    it("无 threadId → 原样返回原数组", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "write_file", true, {
          resultData: { path: "/tmp/a.py" },
        }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, undefined, isUser),
      );
      // 直接返回原始数组，不插入汇总
      expect(result.current).toBe(items);
      expect(result.current).toHaveLength(2);
    });
  });

  // ========== 单轮 & 多轮 ==========

  describe("单轮（一条 user + 若干 tool）→ 末尾插入 files-summary", () => {
    it("一条 user + 两个 tool → 结尾一个 files-summary", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "Write", true, { arguments: { file_path: "/a.ts" } }),
        toolItem("t2", "Edit", true, { arguments: { file_path: "/b.ts" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toEqual(["/a.ts", "/b.ts"]);
      expect(sums[0]!.threadId).toBe("thread-1");
      // files-summary 在末尾
      expect(result.current[result.current.length - 1]).toBe(sums[0]);
    });
  });

  describe("多轮（多条 user 分界）→ 每轮各自插入", () => {
    it("两轮各有文件修改 → 两个 files-summary", () => {
      const items: TestItem[] = [
        // 第一轮
        userItem("u1"),
        toolItem("t1", "write_file", true, { resultData: { path: "/round1.py" } }),
        // 第二轮
        userItem("u2"),
        toolItem("t2", "Write", true, { arguments: { file_path: "/round2.ts" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(2);
      expect(sums[0]!.files).toEqual(["/round1.py"]);
      expect(sums[1]!.files).toEqual(["/round2.ts"]);
      // 每个 summary 的 id 不同
      expect(sums[0]!.id).not.toBe(sums[1]!.id);
    });

    it("多轮顺序正确：user → tool → summary → user → tool → summary", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "Write", true, { arguments: { file_path: "/a.ts" } }),
        userItem("u2"),
        toolItem("t2", "Edit", true, { arguments: { file_path: "/b.ts" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const kinds = result.current.map((it) => (it as { kind: string }).kind);
      expect(kinds).toEqual(["user", "tool", "files-summary", "user", "tool", "files-summary"]);
    });
  });

  // ========== 无文件修改的轮次 → 不插入空汇总 ==========

  describe("无文件修改的轮次 → 不插入空汇总", () => {
    it("只有 assistant 回复、无 tool 写文件 → 不插入 summary", () => {
      const items: TestItem[] = [
        userItem("u1"),
        assistantItem("a1"),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(0);
      expect(result.current).toHaveLength(2);
    });

    it("多轮中只有一轮有文件修改 → 只插入一个 summary", () => {
      const items: TestItem[] = [
        // 第一轮：无文件写入
        userItem("u1"),
        assistantItem("a1"),
        // 第二轮：有文件写入
        userItem("u2"),
        toolItem("t1", "Write", true, { arguments: { file_path: "/only.ts" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toEqual(["/only.ts"]);
    });
  });

  // ========== 同一文件多次写入 → Map 去重 ==========

  describe("同一文件被多个 tool 写入 → Map 去重", () => {
    it("同一文件被 Write + Edit 各写一次 → files 只出现一次", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "Write", true, { arguments: { file_path: "/dup.ts" } }),
        toolItem("t2", "Edit", true, { arguments: { file_path: "/dup.ts" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toEqual(["/dup.ts"]);
    });

    it("三个 tool 写同一文件 + 一个不同文件 → files 只有 2 个", () => {
      const items: TestItem[] = [
        userItem("u1"),
        toolItem("t1", "Write", true, { arguments: { file_path: "/same.ts" } }),
        toolItem("t2", "Edit", true, { arguments: { file_path: "/same.ts" } }),
        toolItem("t3", "MultiEdit", true, { arguments: { file_path: "/same.ts" } }),
        toolItem("t4", "write_file", true, { resultData: { path: "/other.py" } }),
      ];
      const { result } = renderHook(() =>
        useItemsWithFileSummary(items, "thread-1", isUser),
      );
      const sums = summaries(result.current);
      expect(sums).toHaveLength(1);
      expect(sums[0]!.files).toHaveLength(2);
      expect(sums[0]!.files).toContain("/same.ts");
      expect(sums[0]!.files).toContain("/other.py");
    });
  });
});
