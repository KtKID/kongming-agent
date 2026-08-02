/**
 * ThreadPermissionsManager 交互测试。
 *
 * 覆盖加载、空本子、CAS 保存、409 冲突、网络失败重试和 thread 切换隔离。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThreadPermissionsManager } from "../ThreadPermissionsManager";
import type { PermissionRuleDTO } from "@/protocol";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function snapshot(
  threadId: string,
  revision: number,
  allow: PermissionRuleDTO[] = [],
  deny: PermissionRuleDTO[] = [],
) {
  return {
    schema_version: 2 as const,
    thread_id: threadId,
    revision,
    allow,
    deny,
    updated_at: "2026-07-16T08:00:00Z",
    migration_summary: null,
  };
}

function deferredResponse(): {
  promise: Promise<Response>;
  resolve: (response: Response) => void;
} {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("ThreadPermissionsManager", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("加载期间显示 loading，空本子就绪后显示空态", async () => {
    const pending = deferredResponse();
    vi.mocked(fetch).mockReturnValueOnce(pending.promise);

    render(<ThreadPermissionsManager threadId="thread-empty" />);
    expect(screen.getByTestId("thread-permissions-loading")).toBeInTheDocument();

    pending.resolve(jsonResponse(snapshot("thread-empty", 0)));

    expect(await screen.findByTestId("thread-permissions-empty")).toBeInTheDocument();
    expect(screen.getByTestId("thread-permissions-revision")).toHaveTextContent("revision 0");
  });

  it("编辑后按当前 revision PUT，并用服务端快照刷新本地状态", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(snapshot("thread-save", 4)))
      .mockResolvedValueOnce(
        jsonResponse(
          snapshot(
            "thread-save",
            5,
            [
              { expression: "read_file", scope_cwd: null },
              { expression: "run_shell(git status:*)", scope_cwd: "/repo/a" },
            ],
            [{ expression: "run_shell(curl:*)", scope_cwd: null }],
          ),
        ),
      );

    render(<ThreadPermissionsManager threadId="thread-save" />);
    await screen.findByTestId("thread-permissions-empty");
    fireEvent.click(screen.getByRole("button", { name: "添加允许规则" }));
    fireEvent.change(screen.getByLabelText("allow expression 1"), {
      target: { value: "read_file" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加允许规则" }));
    fireEvent.change(screen.getByLabelText("allow expression 2"), {
      target: { value: "run_shell(git status:*)" },
    });
    fireEvent.change(screen.getByLabelText("allow cwd 2"), {
      target: { value: "/repo/a" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加拒绝规则" }));
    fireEvent.change(screen.getByLabelText("deny expression 1"), {
      target: { value: "run_shell(curl:*)" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存审批本子" }));

    await screen.findByText("已保存");
    expect(fetch).toHaveBeenCalledTimes(2);
    const [, init] = vi.mocked(fetch).mock.calls[1];
    expect(init?.method).toBe("PUT");
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    });
    expect(JSON.parse(String(init?.body))).toEqual({
      thread_id: "thread-save",
      revision: 4,
      allow: [
        { expression: "read_file", scope_cwd: null },
        { expression: "run_shell(git status:*)", scope_cwd: "/repo/a" },
      ],
      deny: [{ expression: "run_shell(curl:*)", scope_cwd: null }],
    });
    expect(screen.getByTestId("thread-permissions-revision")).toHaveTextContent("revision 5");
  });

  it("PUT 返回 409 时保留草稿并展示冲突与重新加载入口", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        jsonResponse(
          snapshot("thread-conflict", 2, [{ expression: "read_file", scope_cwd: null }]),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error_code: "permissions_revision_conflict",
            message: "expected revision 2, current revision 3",
          },
          409,
        ),
      );

    render(<ThreadPermissionsManager threadId="thread-conflict" />);
    const allow = await screen.findByLabelText("allow expression 1");
    fireEvent.change(allow, { target: { value: "list_dir" } });
    fireEvent.click(screen.getByRole("button", { name: "保存审批本子" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("revision 冲突");
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    expect(screen.getByLabelText("allow expression 1")).toHaveValue("list_dir");
  });

  it("加载失败显示错误，重试后进入可编辑状态", async () => {
    vi.mocked(fetch)
      .mockRejectedValueOnce(new TypeError("connection reset"))
      .mockResolvedValueOnce(
        jsonResponse(
          snapshot("thread-retry", 1, [{ expression: "read_file", scope_cwd: null }]),
        ),
      );

    render(<ThreadPermissionsManager threadId="thread-retry" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("connection reset");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByLabelText("allow expression 1")).toHaveValue("read_file");
  });

  it("thread 切换立即清空旧草稿，迟到的旧响应不会覆盖新 thread", async () => {
    const oldPending = deferredResponse();
    const nextPending = deferredResponse();
    vi.mocked(fetch)
      .mockReturnValueOnce(oldPending.promise)
      .mockReturnValueOnce(nextPending.promise);

    const { rerender } = render(<ThreadPermissionsManager threadId="thread-a" />);
    rerender(<ThreadPermissionsManager threadId="thread-b" />);
    expect(screen.getByTestId("thread-permissions-loading")).toBeInTheDocument();

    oldPending.resolve(
      jsonResponse(
        snapshot("thread-a", 8, [{ expression: "old-rule", scope_cwd: null }]),
      ),
    );
    nextPending.resolve(
      jsonResponse(
        snapshot("thread-b", 1, [{ expression: "new-rule", scope_cwd: null }]),
      ),
    );

    await waitFor(() => {
      expect(screen.getByLabelText("allow expression 1")).toHaveValue("new-rule");
    });
    expect(screen.queryByDisplayValue("old-rule")).not.toBeInTheDocument();
  });

  it("展示 v1 迁移后失效的 Shell allow 数量", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({
        ...snapshot("thread-migrated", 3),
        migration_summary: {
          from_schema_version: 1,
          to_schema_version: 2,
          invalidated_shell_allow_count: 2,
          backup_path: "/home/safety/thread_permissions/a.v1.bak",
        },
      }),
    );

    render(<ThreadPermissionsManager threadId="thread-migrated" />);

    expect(await screen.findByTestId("thread-permissions-migration-summary")).toHaveTextContent(
      "2 条旧 Shell 允许规则已失效",
    );
  });
});
