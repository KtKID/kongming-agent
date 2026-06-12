/**
 * CodexProjectsTree 单元测试 — projects-registry v0.1（#22 子任务）。
 *
 * 镜像 ClaudeProjectsTree.test.tsx 中的"添加项目 / 移除项目"用例：
 *   - "+ 添加项目" 按钮渲染
 *   - 点按钮 → AddProjectDialog 打开（kind=codex）
 *   - 空列表态文案
 *   - 项目行渲染 🗑 + confirm=true → 调 removeCodexProject
 *   - confirm=false → 不调
 *
 * mock 策略：
 *   - `@/lib/api` 只 mock apiGet（Codex 后端 GET /api/codex/projects）。
 *   - `@/components/AddProjectDialog` 整体替换为简单占位，专注 Tree 行为。
 *   - threads store 用 setState 注入 spy 的 removeCodexProject。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { CodexProjectsTree } from "@/components/CodexProjectsTree";
import * as api from "@/lib/api";
import { useThreadsStore } from "@/stores/threads";

// ---------------------------------------------------------------------------
// AddProjectDialog mock
// ---------------------------------------------------------------------------

vi.mock("@/components/AddProjectDialog", () => ({
  AddProjectDialog: ({
    open,
    kind,
  }: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    kind: string;
  }) =>
    open ? (
      <div
        data-testid="mock-add-project-dialog"
        data-kind={kind}
      >
        AddProjectDialog opened: {kind}
      </div>
    ) : null,
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const fakeCodexProjects = [
  {
    cwd: "/foo/codex",
    display_name: "codex-bar",
    last_modified: Math.floor(Date.now() / 1000) - 60,
    sessions: [
      {
        session_id: "csid-1",
        title: "codex first",
        cwd: "/foo/codex",
        last_modified: Math.floor(Date.now() / 1000) - 60,
        message_count: 7,
        cli_version: "1.0",
        rollout_path: "/foo/codex/rollout.jsonl",
        provider: "openai",
        is_pinned: false,
      },
    ],
  },
];

const removeCodexProject = vi.fn();
const onNewSession = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  removeCodexProject.mockReset();
  removeCodexProject.mockResolvedValue(undefined);
  onNewSession.mockReset();
  useThreadsStore.setState({
    threads: [],
    presets: [],
    loading: false,
    pendingNewSession: null,
    initialMessage: null,
    claudeProjectsRefreshKey: 0,
    codexProjectsRefreshKey: 0,
    addClaudeProject: vi.fn(),
    addCodexProject: vi.fn(),
    removeClaudeProject: vi.fn(),
    removeCodexProject,
  });
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("CodexProjectsTree · projects-registry v0.1", () => {
  it("'+ 添加项目' 按钮渲染", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]);
    render(
      <CodexProjectsTree
        onSessionClick={vi.fn()}
        onNewSession={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /添加项目/ }),
    ).toBeInTheDocument();
  });

  it("点 '+ 添加项目' → AddProjectDialog 打开（kind=codex）", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]);
    render(
      <CodexProjectsTree
        onSessionClick={vi.fn()}
        onNewSession={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("mock-add-project-dialog")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: /添加项目/ }),
    );

    const dialog = await screen.findByTestId("mock-add-project-dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog.dataset.kind).toBe("codex");
  });

  it("空列表态文案为'还没添加任何项目，点上方按钮添加'", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue([]);
    render(
      <CodexProjectsTree
        onSessionClick={vi.fn()}
        onNewSession={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText("还没添加任何项目，点上方按钮添加"),
      ).toBeInTheDocument(),
    );
  });

  it("展开项目后显示'新建会话'按钮，并把项目回传给回调", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(fakeCodexProjects);

    render(
      <MemoryRouter>
        <CodexProjectsTree
          onSessionClick={vi.fn()}
          onNewSession={onNewSession}
        />
      </MemoryRouter>,
    );

    const button = await screen.findByRole("button", { name: "新建会话" });
    await userEvent.click(button);

    expect(onNewSession).toHaveBeenCalledTimes(1);
    expect(onNewSession).toHaveBeenCalledWith(fakeCodexProjects[0]);
  });

  describe("项目节点 🗑 移除按钮", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("项目行渲染移除按钮", async () => {
      vi.spyOn(api, "apiGet").mockResolvedValue(fakeCodexProjects);
      render(
        <MemoryRouter>
          <CodexProjectsTree
            onSessionClick={vi.fn()}
            onNewSession={vi.fn()}
          />
        </MemoryRouter>,
      );
      await waitFor(() =>
        expect(screen.getByText("codex-bar")).toBeInTheDocument(),
      );
      expect(
        screen.getByRole("button", { name: "移除项目 codex-bar" }),
      ).toBeInTheDocument();
    });

    it("点 🗑 + confirm=true → 调 removeCodexProject(project.cwd)", async () => {
      vi.spyOn(api, "apiGet").mockResolvedValue(fakeCodexProjects);
      vi.spyOn(window, "confirm").mockReturnValue(true);

      render(
        <MemoryRouter>
          <CodexProjectsTree
            onSessionClick={vi.fn()}
            onNewSession={vi.fn()}
          />
        </MemoryRouter>,
      );

      await waitFor(() =>
        expect(screen.getByText("codex-bar")).toBeInTheDocument(),
      );

      await userEvent.click(
        screen.getByRole("button", { name: "移除项目 codex-bar" }),
      );

      await waitFor(() => {
        expect(removeCodexProject).toHaveBeenCalledWith("/foo/codex");
      });
    });

    it("点 🗑 + confirm=false → 不调 removeCodexProject", async () => {
      vi.spyOn(api, "apiGet").mockResolvedValue(fakeCodexProjects);
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

      render(
        <MemoryRouter>
          <CodexProjectsTree
            onSessionClick={vi.fn()}
            onNewSession={vi.fn()}
          />
        </MemoryRouter>,
      );

      await waitFor(() =>
        expect(screen.getByText("codex-bar")).toBeInTheDocument(),
      );

      await userEvent.click(
        screen.getByRole("button", { name: "移除项目 codex-bar" }),
      );

      await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
      expect(removeCodexProject).not.toHaveBeenCalled();
    });
  });
});
