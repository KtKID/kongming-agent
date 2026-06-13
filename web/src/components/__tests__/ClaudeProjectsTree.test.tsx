import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { toast } from "sonner";
import { ClaudeProjectsTree } from "@/components/ClaudeProjectsTree";
import * as api from "@/lib/api";
import { useThreadsStore } from "@/stores/threads";

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// AddProjectDialog 是独立组件，单独有 AddProjectDialog.test.tsx 覆盖；
// 这里 mock 掉它，专注验证 Tree 上"+ 添加项目"按钮接线 + 项目移除回调。
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

const fakeProjects = {
  projects: [
    {
      name: "-foo-bar",
      cwd: "/foo/bar",
      display_name: "bar",
      sessions: [
        {
          claude_thread_id: "sid-1",
          title: "first session title",
          last_modified: Math.floor(Date.now() / 1000) - 60,
          message_count: 42,
          is_pinned: false,
        },
        {
          claude_thread_id: "sid-2",
          title: "second session",
          last_modified: Math.floor(Date.now() / 1000) - 7200,
          message_count: 10,
          is_pinned: false,
        },
      ],
    },
    {
      name: "-old-project",
      cwd: "/old/project",
      display_name: "project",
      sessions: [
        {
          claude_thread_id: "sid-old",
          title: "old session",
          last_modified: Math.floor(Date.now() / 1000) - 7 * 86400 - 100,
          message_count: 5,
          is_pinned: false,
        },
      ],
    },
  ],
};

describe("ClaudeProjectsTree", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loading 态显示", () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockImplementation(
      () => new Promise(() => {}),  // 永不 resolve
    );
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    expect(screen.getByText(/正在扫描/)).toBeInTheDocument();
  });

  it("error 态显示", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockRejectedValue(
      new Error("network down"),
    );
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/network down/)).toBeInTheDocument(),
    );
  });

  it("empty 态显示新文案", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue({
      projects: [],
    });
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(
        screen.getByText("还没添加任何项目，点上方按钮添加"),
      ).toBeInTheDocument(),
    );
  });

  it("渲染项目 + 自动展开最近一周内有活动的项目", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    // 项目名渲染
    await waitFor(() =>
      expect(screen.getByText("bar")).toBeInTheDocument(),
    );
    expect(screen.getByText("project")).toBeInTheDocument();
    // 最近 1 周有活动的 -foo-bar 项目自动展开 → session title 可见
    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );
    // 旧项目折叠状态 → 其 session 不渲染
    expect(screen.queryByText("old session")).toBeNull();
  });

  it("点击 session 卡片触发 onSessionClick(project, session)", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
    const onSessionClick = vi.fn();
    render(<ClaudeProjectsTree onSessionClick={onSessionClick} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByText("first session title"));
    expect(onSessionClick).toHaveBeenCalledTimes(1);
    const [project, session] = onSessionClick.mock.calls[0]!;
    expect(project.name).toBe("-foo-bar");
    expect(session.claude_thread_id).toBe("sid-1");
  });

  it("搜索会即时过滤并自动展开匹配项目", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByText("bar")).toBeInTheDocument(),
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: "搜索 Claude 历史会话" }),
      "old",
    );

    await waitFor(() =>
      expect(screen.getByText("old session")).toBeInTheDocument(),
    );
    expect(screen.queryByText("first session title")).toBeNull();
  });

  it("支持全部展开和全部收起", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByText("bar")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "全部展开" }));
    await waitFor(() =>
      expect(screen.getByText("old session")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "全部收起" }));
    await waitFor(() =>
      expect(screen.queryByText("first session title")).toBeNull(),
    );
    expect(screen.queryByText("old session")).toBeNull();
  });

  it("重新加载项目时显示进度并更新列表", async () => {
    const refreshSpy = vi
      .spyOn(api, "apiRefreshClaudeProjects")
      .mockResolvedValueOnce(fakeProjects)
      .mockImplementationOnce(async (onProgress) => {
        onProgress({
          current: 1,
          total: 2,
          current_project: "-foo-bar",
        });
        await new Promise((resolve) => setTimeout(resolve, 20));
        return {
          projects: [
            ...fakeProjects.projects,
            {
              name: "-new-project",
              cwd: "/new/project",
              display_name: "new-project",
              sessions: [
                {
                  claude_thread_id: "sid-new",
                  title: "fresh session",
                  last_modified: Math.floor(Date.now() / 1000),
                  message_count: 3,
                  is_pinned: false,
                },
              ],
            },
          ],
        };
      });

    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText("bar")).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect(refreshSpy).toHaveBeenCalledTimes(2);
    await waitFor(() =>
      expect(screen.getByText("1/2")).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByText("fresh session")).toBeInTheDocument(),
    );
  });

  it("初次进入 Claude tab 就显示刷新进度卡", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockImplementation(
      async (onProgress) => {
        onProgress({
          current: 1,
          total: 2,
          current_project: "-foo-bar",
        });
        await new Promise((resolve) => setTimeout(resolve, 20));
        return fakeProjects;
      },
    );

    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);

    expect(screen.getByText(/扫描：/)).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );
  });
});

// ---------------------------------------------------------------------------
// projects-registry v0.1: 添加项目 / 移除项目（#22 子任务）
// ---------------------------------------------------------------------------

describe("ClaudeProjectsTree · projects-registry v0.1", () => {
  const removeClaudeProject = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    removeClaudeProject.mockReset();
    removeClaudeProject.mockResolvedValue(undefined);
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
      removeClaudeProject,
      removeCodexProject: vi.fn(),
    });
  });

  it("'+ 添加项目' 按钮渲染", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue({ projects: [] });
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    expect(
      screen.getByRole("button", { name: /添加项目/ }),
    ).toBeInTheDocument();
  });

  it("点 '+ 添加项目' → AddProjectDialog 打开（kind=claude）", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue({ projects: [] });
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);

    // 初始 dialog 关闭
    expect(screen.queryByTestId("mock-add-project-dialog")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: /添加项目/ }),
    );

    const dialog = await screen.findByTestId("mock-add-project-dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog.dataset.kind).toBe("claude");
  });

  it("空列表态文案为'还没添加任何项目，点上方按钮添加'", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue({ projects: [] });
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(
        screen.getByText("还没添加任何项目，点上方按钮添加"),
      ).toBeInTheDocument(),
    );
  });

  describe("项目节点 🗑 移除按钮", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("项目行渲染移除按钮", async () => {
      vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
      render(
        <MemoryRouter>
          <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
        </MemoryRouter>,
      );
      await waitFor(() =>
        expect(screen.getByText("bar")).toBeInTheDocument(),
      );
      // renderProjectActions 注入的移除按钮，aria-label="移除项目 <display_name>"
      expect(
        screen.getByRole("button", { name: "移除项目 bar" }),
      ).toBeInTheDocument();
    });

    it("点 🗑 + confirm=true → 调 removeClaudeProject(project.cwd)", async () => {
      vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
      vi.spyOn(window, "confirm").mockReturnValue(true);

      render(
        <MemoryRouter>
          <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
        </MemoryRouter>,
      );

      await waitFor(() =>
        expect(screen.getByText("bar")).toBeInTheDocument(),
      );

      await userEvent.click(
        screen.getByRole("button", { name: "移除项目 bar" }),
      );

      await waitFor(() => {
        expect(removeClaudeProject).toHaveBeenCalledWith("/foo/bar");
      });
    });

    it("点 🗑 + confirm=false → 不调 removeClaudeProject", async () => {
      vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(fakeProjects);
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

      render(
        <MemoryRouter>
          <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
        </MemoryRouter>,
      );

      await waitFor(() =>
        expect(screen.getByText("bar")).toBeInTheDocument(),
      );

      await userEvent.click(
        screen.getByRole("button", { name: "移除项目 bar" }),
      );

      // confirm 调到 → cancelled，不会 await store
      await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
      expect(removeClaudeProject).not.toHaveBeenCalled();
    });
  });
});

// ---------------------------------------------------------------------------
// claude-session-rename-archive-metadata-source: rename / archive 走
// `POST /api/threads/import-claude-session` + `PATCH /api/threads/{id}`
// 双调用样板（取代旧 `/api/claude/sessions/rename` / `archive` 端点）。
// ---------------------------------------------------------------------------

describe("ClaudeProjectsTree · rename + archive 调 import-then-patch 双调用", () => {
  const fakeThread = {
    id: "thread-abcdef012345",
    name: "first session title",
    preset_id: "",
    backend_kind: "claude_code" as const,
    claude_thread_id: "sid-1",
    codex_thread_id: "",
    cwd: "/foo/bar",
    created_at: Math.floor(Date.now() / 1000),
    updated_at: Math.floor(Date.now() / 1000),
    message_count: 42,
    is_pinned: false,
    is_archived: false,
    schema_version: 10 as const,
  };

  // rename onConfirm 会直接改 session.title（mutate fakeProjects 共享对象），
  // 用 structuredClone 拷一份独立的 projects 给 spy 返回，避免污染后续用例。
  const cloneFakeProjects = (): typeof fakeProjects =>
    structuredClone(fakeProjects);

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("rename 流程：先 POST import-claude-session，再 PATCH /api/threads/{id} body={name}", async () => {
    const apiRefreshSpy = vi
      .spyOn(api, "apiRefreshClaudeProjects")
      .mockResolvedValue(cloneFakeProjects());
    const apiPostSpy = vi
      .spyOn(api, "apiPost")
      .mockResolvedValue({ thread: fakeThread, imported: true });
    const apiPatchSpy = vi
      .spyOn(api, "apiPatch")
      .mockResolvedValue(undefined);

    render(
      <MemoryRouter>
        <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );

    // 点 "重命名"（aria-label 由 SidebarSessionRow 注入 = action.label）
    const renameButtons = screen.getAllByRole("button", { name: "重命名" });
    await userEvent.click(renameButtons[0]!);

    // input 出现，输入新名 + Enter 确认
    const input = await screen.findByDisplayValue("first session title");
    await userEvent.clear(input);
    await userEvent.type(input, "new title{Enter}");

    // 1. 必须先调 import-claude-session
    await waitFor(() => {
      expect(apiPostSpy).toHaveBeenCalledWith(
        "/api/threads/import-claude-session",
        expect.objectContaining({
          claude_thread_id: "sid-1",
          cwd: "/foo/bar",
          name: "first session title",
        }),
      );
    });

    // 2. 再调 PATCH /api/threads/{id} body={name: "new title"}
    await waitFor(() => {
      expect(apiPatchSpy).toHaveBeenCalledWith(
        `/api/threads/${fakeThread.id}`,
        { name: "new title" },
      );
    });

    // 3. 旧端点 `/api/claude/sessions/rename` 不应再被调用
    expect(apiPatchSpy).not.toHaveBeenCalledWith(
      "/api/claude/sessions/rename",
      expect.anything(),
    );

    // 4. 首次 rename（imported=true）必须 refresh，让左侧 threads 列表立即同步
    //    （否则 phase / status / pin 查询读 stale 值直到下次手动刷新）
    await waitFor(() => {
      // mock 默认是初始加载触发的一次 + rename 完触发的一次 = 2 次
      expect(apiRefreshSpy.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("archive 流程：confirm=true → 先 POST import-claude-session，再 PATCH /api/threads/{id} body={is_archived:true}", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(cloneFakeProjects());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const apiPostSpy = vi
      .spyOn(api, "apiPost")
      .mockResolvedValue({ thread: fakeThread, imported: true });
    const apiPatchSpy = vi
      .spyOn(api, "apiPatch")
      .mockResolvedValue(undefined);

    render(
      <MemoryRouter>
        <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );

    // 点 "归档"
    const archiveButtons = screen.getAllByRole("button", { name: "归档" });
    await userEvent.click(archiveButtons[0]!);

    // 1. 必须先调 import-claude-session
    await waitFor(() => {
      expect(apiPostSpy).toHaveBeenCalledWith(
        "/api/threads/import-claude-session",
        expect.objectContaining({
          claude_thread_id: "sid-1",
          cwd: "/foo/bar",
          name: "first session title",
        }),
      );
    });

    // 2. 再调 PATCH /api/threads/{id} body={is_archived: true}
    await waitFor(() => {
      expect(apiPatchSpy).toHaveBeenCalledWith(
        `/api/threads/${fakeThread.id}`,
        { is_archived: true },
      );
    });

    // 3. 旧端点 `/api/claude/sessions/archive` 不应再被调用
    expect(apiPatchSpy).not.toHaveBeenCalledWith(
      "/api/claude/sessions/archive",
      expect.anything(),
    );
  });

  it("archive 流程：confirm=false → 不调任何 API", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(cloneFakeProjects());
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const apiPostSpy = vi.spyOn(api, "apiPost");
    const apiPatchSpy = vi.spyOn(api, "apiPatch");

    render(
      <MemoryRouter>
        <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );

    const archiveButtons = screen.getAllByRole("button", { name: "归档" });
    await userEvent.click(archiveButtons[0]!);

    // confirm 拒绝 → 不会调 import / patch
    expect(apiPostSpy).not.toHaveBeenCalled();
    expect(apiPatchSpy).not.toHaveBeenCalled();
  });

  it("rename 错误路径：apiPost reject → toast.error 弹错误消息", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(cloneFakeProjects());
    vi.spyOn(api, "apiPost").mockRejectedValue(new Error("network down"));
    const toastErrorSpy = vi.mocked(toast.error);

    render(
      <MemoryRouter>
        <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );

    const renameButtons = screen.getAllByRole("button", { name: "重命名" });
    await userEvent.click(renameButtons[0]!);
    const input = await screen.findByDisplayValue("first session title");
    await userEvent.clear(input);
    await userEvent.type(input, "new title{Enter}");

    await waitFor(() => {
      expect(toastErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining("重命名失败"),
      );
    });
  });

  it("archive 错误路径：apiPatch reject → toast.error 弹错误消息", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue(cloneFakeProjects());
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(api, "apiPost").mockResolvedValue({
      thread: fakeThread,
      imported: true,
    });
    vi.spyOn(api, "apiPatch").mockRejectedValue(new Error("server 500"));
    const toastErrorSpy = vi.mocked(toast.error);

    render(
      <MemoryRouter>
        <ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );

    const archiveButtons = screen.getAllByRole("button", { name: "归档" });
    await userEvent.click(archiveButtons[0]!);

    await waitFor(() => {
      expect(toastErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining("归档失败"),
      );
    });
  });
});
