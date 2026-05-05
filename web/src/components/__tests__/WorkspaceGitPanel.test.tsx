import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { WorkspaceGitPanel } from "@/components/WorkspaceGitPanel";
import type {
  WorkspaceGitActionResultDTO,
  WorkspaceContextDTO,
  WorkspaceGitBranchesDTO,
  WorkspaceGitCommitsDTO,
  WorkspaceGitFileDiffDTO,
  WorkspaceGitStatusDTO,
} from "@/protocol";

const toastMocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: toastMocks,
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const claudeContext: WorkspaceContextDTO = {
  thread_id: "thread-1",
  backend_kind: "claude_code",
  workspace_root: "/tmp/proj",
  sdk_session_id: "sdk-1",
  shell_provider: "claude_code",
  files_available: true,
  shell_available: true,
  unavailable_reason: null,
};

describe("WorkspaceGitPanel", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
    toastMocks.success.mockReset();
    toastMocks.error.mockReset();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("显示 changes 视图并支持跳转到 Files", async () => {
    const onOpenFile = vi.fn();
    globalThis.fetch = vi.fn((input) => {
      const url = String(input);
      if (url.endsWith("/workspace-git/status")) {
        return Promise.resolve(
          jsonResponse({
            workspace_root: "/tmp/proj",
            repo_root: "/tmp/proj",
            current_branch: "main",
            tracking_branch: "origin/main",
            ahead_count: 1,
            behind_count: 0,
            changes: [
              {
                path: "src/app.ts",
                name: "app.ts",
                staged_status: "M",
                unstaged_status: " ",
                previous_path: null,
              },
            ],
          } satisfies WorkspaceGitStatusDTO),
        );
      }
      if (url.endsWith("/workspace-git/branches")) {
        return Promise.resolve(
          jsonResponse({
            current_branch: "main",
            local_branches: ["main"],
            remote_branches: ["origin/main"],
          } satisfies WorkspaceGitBranchesDTO),
        );
      }
      if (url.endsWith("/workspace-git/commits")) {
        return Promise.resolve(
          jsonResponse({
            commits: [
              {
                commit: "abc",
                short_commit: "abc",
                author: "kid",
                authored_at: "2026-05-03T00:00:00+08:00",
                subject: "init",
              },
            ],
          } satisfies WorkspaceGitCommitsDTO),
        );
      }
      if (url.includes("/workspace-git/file-diff?path=src%2Fapp.ts")) {
        return Promise.resolve(
          jsonResponse({
            path: "src/app.ts",
            diff: "diff --git a/src/app.ts b/src/app.ts\n+hello",
          } satisfies WorkspaceGitFileDiffDTO),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceGitPanel context={claudeContext} onOpenFile={onOpenFile} />);

    expect(await screen.findByText("1 个改动文件")).toBeInTheDocument();
    expect(await screen.findAllByText("src/app.ts")).toHaveLength(2);
    expect(await screen.findByText(/diff --git/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /在 Files 中打开/ }));
    expect(onOpenFile).toHaveBeenCalledWith("src/app.ts");
  });

  it("切换到 history 和 branches 视图", async () => {
    globalThis.fetch = vi.fn((input) => {
      const url = String(input);
      if (url.endsWith("/workspace-git/status")) {
        return Promise.resolve(
          jsonResponse({
            workspace_root: "/tmp/proj",
            repo_root: "/tmp/proj",
            current_branch: "main",
            tracking_branch: null,
            ahead_count: 0,
            behind_count: 0,
            changes: [],
          } satisfies WorkspaceGitStatusDTO),
        );
      }
      if (url.endsWith("/workspace-git/branches")) {
        return Promise.resolve(
          jsonResponse({
            current_branch: "main",
            local_branches: ["main", "feature/demo"],
            remote_branches: ["origin/main"],
          } satisfies WorkspaceGitBranchesDTO),
        );
      }
      if (url.endsWith("/workspace-git/commits")) {
        return Promise.resolve(
          jsonResponse({
            commits: [
              {
                commit: "abc",
                short_commit: "abc",
                author: "kid",
                authored_at: "2026-05-03T00:00:00+08:00",
                subject: "init",
              },
            ],
          } satisfies WorkspaceGitCommitsDTO),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceGitPanel context={claudeContext} />);

    fireEvent.click(await screen.findByRole("button", { name: "History" }));
    expect(await screen.findByText("Recent commits")).toBeInTheDocument();
    expect(await screen.findByText("init")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Branches" }));
    await waitFor(() => {
      expect(screen.getByText("Local branches")).toBeInTheDocument();
      expect(screen.getByText("feature/demo")).toBeInTheDocument();
    });
  });

  it("支持真实 stage / unstage / commit 写操作", async () => {
    let statusBody: WorkspaceGitStatusDTO = {
      workspace_root: "/tmp/proj",
      repo_root: "/tmp/proj",
      current_branch: "main",
      tracking_branch: "origin/main",
      ahead_count: 0,
      behind_count: 0,
      changes: [
        {
          path: "src/app.ts",
          name: "app.ts",
          staged_status: " ",
          unstaged_status: "M",
          previous_path: null,
        },
      ],
    };
    let commitsBody: WorkspaceGitCommitsDTO = { commits: [] };

    globalThis.fetch = vi.fn((input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url.endsWith("/workspace-git/status")) {
        return Promise.resolve(jsonResponse(statusBody));
      }
      if (method === "GET" && url.endsWith("/workspace-git/branches")) {
        return Promise.resolve(
          jsonResponse({
            current_branch: "main",
            local_branches: ["main"],
            remote_branches: ["origin/main"],
          } satisfies WorkspaceGitBranchesDTO),
        );
      }
      if (method === "GET" && url.endsWith("/workspace-git/commits")) {
        return Promise.resolve(jsonResponse(commitsBody));
      }
      if (method === "GET" && url.includes("/workspace-git/file-diff?path=src%2Fapp.ts")) {
        return Promise.resolve(
          jsonResponse({
            path: "src/app.ts",
            diff: "diff --git a/src/app.ts b/src/app.ts\n+hello",
          } satisfies WorkspaceGitFileDiffDTO),
        );
      }
      if (method === "POST" && url.endsWith("/workspace-git/stage")) {
        statusBody = {
          ...statusBody,
          changes: [
            {
              path: "src/app.ts",
              name: "app.ts",
              staged_status: "M",
              unstaged_status: " ",
              previous_path: null,
            },
          ],
        };
        return Promise.resolve(
          jsonResponse({ detail: "staged 1 path(s)" } satisfies WorkspaceGitActionResultDTO),
        );
      }
      if (method === "POST" && url.endsWith("/workspace-git/unstage")) {
        statusBody = {
          ...statusBody,
          changes: [
            {
              path: "src/app.ts",
              name: "app.ts",
              staged_status: " ",
              unstaged_status: "M",
              previous_path: null,
            },
          ],
        };
        return Promise.resolve(
          jsonResponse({ detail: "unstaged 1 path(s)" } satisfies WorkspaceGitActionResultDTO),
        );
      }
      if (method === "POST" && url.endsWith("/workspace-git/commit")) {
        statusBody = {
          ...statusBody,
          changes: [],
        };
        commitsBody = {
          commits: [
            {
              commit: "abc123",
              short_commit: "abc123",
              author: "kid",
              authored_at: "2026-05-03T00:00:00+08:00",
              subject: "feat: update app",
            },
          ],
        };
        return Promise.resolve(
          jsonResponse({
            detail: "committed abc123",
            commit: "abc123",
            short_commit: "abc123",
            current_branch: "main",
          } satisfies WorkspaceGitActionResultDTO),
        );
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceGitPanel context={claudeContext} />);

    expect(await screen.findByText("1 个改动文件")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "全部暂存" }));
    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith("已暂存全部改动");
      expect(screen.getByText("1 个路径已暂存")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "全部撤出" }));
    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith("已撤出全部暂存");
    });

    fireEvent.click(screen.getByRole("button", { name: "全部暂存" }));
    await waitFor(() => {
      expect(screen.getByText("1 个路径已暂存")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText("输入 commit message"), {
      target: { value: "feat: update app" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));

    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith("已提交 abc123");
      expect(screen.getByText("当前 workspace 很干净")).toBeInTheDocument();
      expect(screen.getByText("选择一个改动文件")).toBeInTheDocument();
    });
  });

  it("支持创建分支并切换现有分支", async () => {
    let branchesBody: WorkspaceGitBranchesDTO = {
      current_branch: "main",
      local_branches: ["main", "feature/demo"],
      remote_branches: ["origin/main"],
    };

    globalThis.fetch = vi.fn((input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method === "GET" && url.endsWith("/workspace-git/status")) {
        return Promise.resolve(
          jsonResponse({
            workspace_root: "/tmp/proj",
            repo_root: "/tmp/proj",
            current_branch: branchesBody.current_branch,
            tracking_branch: null,
            ahead_count: 0,
            behind_count: 0,
            changes: [],
          } satisfies WorkspaceGitStatusDTO),
        );
      }
      if (method === "GET" && url.endsWith("/workspace-git/branches")) {
        return Promise.resolve(jsonResponse(branchesBody));
      }
      if (method === "GET" && url.endsWith("/workspace-git/commits")) {
        return Promise.resolve(jsonResponse({ commits: [] } satisfies WorkspaceGitCommitsDTO));
      }
      if (method === "POST" && url.endsWith("/workspace-git/create-branch")) {
        branchesBody = {
          current_branch: "feature/new-ui",
          local_branches: ["main", "feature/demo", "feature/new-ui"],
          remote_branches: ["origin/main"],
        };
        return Promise.resolve(
          jsonResponse({
            detail: "created branch feature/new-ui",
            current_branch: "feature/new-ui",
          } satisfies WorkspaceGitActionResultDTO),
        );
      }
      if (method === "POST" && url.endsWith("/workspace-git/checkout")) {
        branchesBody = {
          ...branchesBody,
          current_branch: "feature/demo",
        };
        return Promise.resolve(
          jsonResponse({
            detail: "checked out feature/demo",
            current_branch: "feature/demo",
          } satisfies WorkspaceGitActionResultDTO),
        );
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceGitPanel context={claudeContext} />);

    fireEvent.click(await screen.findByRole("button", { name: "Branches" }));

    fireEvent.change(await screen.findByPlaceholderText("feature/..."), {
      target: { value: "feature/new-ui" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith("已创建并切换到 feature/new-ui");
      expect(screen.getByText("feature/new-ui")).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole("button", { name: "切换" })[0]);
    await waitFor(() => {
      expect(toastMocks.success).toHaveBeenCalledWith("已切到 feature/demo");
      expect(screen.getByText("current")).toBeInTheDocument();
    });
  });
});
