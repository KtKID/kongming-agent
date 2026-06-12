/**
 * AddProjectDialog 单元测试（v0.1: projects-registry #22）。
 *
 * 三条主链路：
 *   1. Tauri 选定 → 直接 add → toast.success → 关弹窗（不渲染 fallback UI）。
 *   2. Tauri 取消 → 关弹窗（不渲染 fallback UI）。
 *   3. 浏览器（pickDirectory 返 null + isTauriEnv=false）→ 渲染 fallback UI →
 *      校验 cwd 必须以 / 开头 + 📍 一键填入 + 提交 + onAdded + close。
 *
 * mock 策略：
 *   - `@/lib/dirPicker` 整体 mock；用 `vi.mocked` 控制 isTauriEnv / pickDirectory
 *     / getServerRepoRoot 在每个用例的返回值。
 *   - `sonner` 的 toast 整体 mock，断言 success/error 被调用。
 *   - threads store 用真实 zustand，但通过 setState 注入 spy 版的
 *     addClaudeProject / addCodexProject。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { AddProjectDialog } from "@/components/AddProjectDialog";
import { useThreadsStore } from "@/stores/threads";
import * as dirPicker from "@/lib/dirPicker";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock("@/lib/dirPicker", () => ({
  isTauriEnv: vi.fn(),
  pickDirectory: vi.fn(),
  getServerRepoRoot: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const addClaudeProject = vi.fn();
const addCodexProject = vi.fn();

const fakeProject = {
  cwd: "/foo/bar",
  alias: "",
  added_at: 0,
};

beforeEach(() => {
  vi.clearAllMocks();
  addClaudeProject.mockReset();
  addCodexProject.mockReset();
  addClaudeProject.mockResolvedValue(fakeProject);
  addCodexProject.mockResolvedValue(fakeProject);

  useThreadsStore.setState({
    threads: [],
    presets: [],
    loading: false,
    pendingNewSession: null,
    initialMessage: null,
    claudeProjectsRefreshKey: 0,
    codexProjectsRefreshKey: 0,
    addClaudeProject,
    addCodexProject,
    removeClaudeProject: vi.fn(),
    removeCodexProject: vi.fn(),
  });
});

// ---------------------------------------------------------------------------
// Tauri 分支 — pickDirectory 返字符串
// ---------------------------------------------------------------------------

describe("AddProjectDialog · Tauri picker selected", () => {
  it("选定 → 调 addClaudeProject(cwd, '') + toast.success + 关弹窗 + 不渲染 fallback", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(true);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue("/Users/me/proj");

    const onOpenChange = vi.fn();
    const onAdded = vi.fn();

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={onOpenChange}
        kind="claude"
        onAdded={onAdded}
      />,
    );

    await waitFor(() => {
      expect(addClaudeProject).toHaveBeenCalledWith("/Users/me/proj", "");
    });

    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });

    expect(toast.success).toHaveBeenCalledWith("项目已添加");
    expect(onAdded).toHaveBeenCalledWith("/Users/me/proj");
    // fallback UI 不渲染（输入框不存在）
    expect(screen.queryByLabelText("cwd")).toBeNull();
  });

  it("addClaudeProject 抛错 → toast.error + onOpenChange(false)", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(true);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue("/Users/me/proj");
    addClaudeProject.mockRejectedValueOnce(new Error("server down"));

    const onOpenChange = vi.fn();

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={onOpenChange}
        kind="claude"
      />,
    );

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("添加失败：server down");
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(toast.success).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Tauri 分支 — pickDirectory 返 null（用户取消）
// ---------------------------------------------------------------------------

describe("AddProjectDialog · Tauri picker cancelled", () => {
  it("isTauriEnv=true + picker null → onOpenChange(false) 且不渲染 fallback", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(true);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    const onOpenChange = vi.fn();
    render(
      <AddProjectDialog
        open={true}
        onOpenChange={onOpenChange}
        kind="claude"
      />,
    );

    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });

    expect(addClaudeProject).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("cwd")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 浏览器 fallback 分支
// ---------------------------------------------------------------------------

describe("AddProjectDialog · browser fallback", () => {
  it("渲染 fallback UI（标题 + cwd 输入 + 📍 + alias + 提交/取消）", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={vi.fn()}
        kind="claude"
      />,
    );

    // 标题渲染
    expect(
      await screen.findByText("添加 Claude 项目"),
    ).toBeInTheDocument();
    // cwd 输入框
    expect(screen.getByLabelText("cwd")).toBeInTheDocument();
    // 📍 一键按钮
    expect(
      screen.getByRole("button", { name: "📍" }),
    ).toBeInTheDocument();
    // alias 输入框
    expect(screen.getByLabelText("alias")).toBeInTheDocument();
    // 提交按钮
    expect(
      screen.getByRole("button", { name: "添加" }),
    ).toBeInTheDocument();
    // 取消按钮
    expect(
      screen.getByRole("button", { name: "取消" }),
    ).toBeInTheDocument();
  });

  it("相对路径 → 提交按钮 disabled + 校验提示", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={vi.fn()}
        kind="claude"
      />,
    );

    const cwdInput = await screen.findByLabelText("cwd");
    await userEvent.type(cwdInput, "relative/path");

    expect(screen.getByRole("button", { name: "添加" })).toBeDisabled();
    expect(
      screen.getByText(/工作目录需要绝对路径/),
    ).toBeInTheDocument();
  });

  it("点 📍 → getServerRepoRoot → 自动填 cwd", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);
    vi.mocked(dirPicker.getServerRepoRoot).mockResolvedValue("/srv/repo");

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={vi.fn()}
        kind="claude"
      />,
    );

    await screen.findByLabelText("cwd");
    await userEvent.click(
      screen.getByRole("button", { name: "📍" }),
    );

    await waitFor(() =>
      expect(dirPicker.getServerRepoRoot).toHaveBeenCalledTimes(1),
    );
    await waitFor(() => {
      const cwdInput = screen.getByLabelText("cwd") as HTMLInputElement;
      expect(cwdInput.value).toBe("/srv/repo");
    });
  });

  it("合法绝对路径 → 提交 → 调 addClaudeProject + 关弹窗", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    const onOpenChange = vi.fn();
    const onAdded = vi.fn();

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={onOpenChange}
        kind="claude"
        onAdded={onAdded}
      />,
    );

    const cwdInput = await screen.findByLabelText("cwd");
    await userEvent.type(cwdInput, "/abs/path");
    const aliasInput = screen.getByLabelText("alias");
    await userEvent.type(aliasInput, "MyProj");

    const submit = screen.getByRole("button", { name: "添加" });
    expect(submit).not.toBeDisabled();
    await userEvent.click(submit);

    await waitFor(() => {
      expect(addClaudeProject).toHaveBeenCalledWith("/abs/path", "MyProj");
    });
    await waitFor(() =>
      expect(onOpenChange).toHaveBeenCalledWith(false),
    );
    expect(toast.success).toHaveBeenCalledWith("项目已添加");
    expect(onAdded).toHaveBeenCalledWith("/abs/path");
  });

  it("kind=codex → 调 addCodexProject 而非 addClaudeProject", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={vi.fn()}
        kind="codex"
      />,
    );

    // 标题切到 Codex
    expect(
      await screen.findByText("添加 Codex 项目"),
    ).toBeInTheDocument();

    const cwdInput = screen.getByLabelText("cwd");
    await userEvent.type(cwdInput, "/abs/codex");
    await userEvent.click(screen.getByRole("button", { name: "添加" }));

    await waitFor(() => {
      expect(addCodexProject).toHaveBeenCalledWith("/abs/codex", "");
    });
    expect(addClaudeProject).not.toHaveBeenCalled();
  });

  it("Windows absolute path submits", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);

    render(
      <AddProjectDialog
        open={true}
        onOpenChange={vi.fn()}
        kind="claude"
      />,
    );

    const cwdInput = await screen.findByLabelText("cwd");
    await userEvent.type(
      cwdInput,
      "E:\\xgt\\proj\\agent-proj\\kongming-agent",
    );
    await userEvent.click(screen.getByRole("button", { name: /添加/ }));

    await waitFor(() => {
      expect(addClaudeProject).toHaveBeenCalledWith(
        "E:\\xgt\\proj\\agent-proj\\kongming-agent",
        "",
      );
    });
  });
});
