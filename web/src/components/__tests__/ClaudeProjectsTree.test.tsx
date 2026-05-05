import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ClaudeProjectsTree } from "@/components/ClaudeProjectsTree";
import * as api from "@/lib/api";

const fakeProjects = {
  projects: [
    {
      name: "-foo-bar",
      cwd: "/foo/bar",
      display_name: "bar",
      sessions: [
        {
          sdk_session_id: "sid-1",
          title: "first session title",
          last_modified: Math.floor(Date.now() / 1000) - 60,
          message_count: 42,
        },
        {
          sdk_session_id: "sid-2",
          title: "second session",
          last_modified: Math.floor(Date.now() / 1000) - 7200,
          message_count: 10,
        },
      ],
    },
    {
      name: "-old-project",
      cwd: "/old/project",
      display_name: "project",
      sessions: [
        {
          sdk_session_id: "sid-old",
          title: "old session",
          last_modified: Math.floor(Date.now() / 1000) - 7 * 86400 - 100,
          message_count: 5,
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
    expect(screen.getByText(/加载项目中/)).toBeInTheDocument();
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

  it("empty 态显示", async () => {
    vi.spyOn(api, "apiRefreshClaudeProjects").mockResolvedValue({
      projects: [],
    });
    render(<ClaudeProjectsTree onSessionClick={vi.fn()} onNewSession={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/尚无 Claude 历史会话/)).toBeInTheDocument(),
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
    expect(session.sdk_session_id).toBe("sid-1");
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
                  sdk_session_id: "sid-new",
                  title: "fresh session",
                  last_modified: Math.floor(Date.now() / 1000),
                  message_count: 3,
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
      expect(screen.getByText("1/2 项目")).toBeInTheDocument(),
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

    expect(screen.getByText("加载项目中...")).toBeInTheDocument();
    expect(screen.getByText("1/2 项目")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("first session title")).toBeInTheDocument(),
    );
  });
});
