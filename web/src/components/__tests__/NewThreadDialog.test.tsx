import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { NewThreadDialog } from "@/components/NewThreadDialog";
import { useThreadsStore } from "@/stores/threads";
import * as dirPicker from "@/lib/dirPicker";
import { toast } from "sonner";

const createThread = vi.fn();

vi.mock("@/lib/dirPicker", () => ({
  isTauriEnv: vi.fn(),
  pickDirectory: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: {
    error: vi.fn(),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  createThread.mockReset();
  vi.mocked(dirPicker.isTauriEnv).mockReturnValue(false);
  vi.mocked(dirPicker.pickDirectory).mockResolvedValue(null);
  useThreadsStore.setState({
    threads: [],
    presets: [
      {
        id: "preset-a",
        display_name: "GPT-4o",
        model: "gpt-4o",
        base_url_summary: "api.openai.com",
        requires_api_key: true,
      },
    ],
    loading: false,
    createThread,
    renameThread: vi.fn(),
    deleteThread: vi.fn(),
    fetchThreads: vi.fn(),
    fetchPresets: vi.fn(),
  });
});

describe("NewThreadDialog", () => {
  it("工作目录输入框右侧显示文件夹选择按钮", () => {
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("button", { name: "打开工作目录选择器" }),
    ).toBeInTheDocument();
  });

  it("默认 backend=generic_chat 时提交带 preset", async () => {
    createThread.mockResolvedValue({
      id: "thread-xyz",
      name: "test",
      preset_id: "preset-a",
      backend_kind: "generic_chat",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
    });
    const onOpenChange = vi.fn();
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={onOpenChange} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "my thread");
    await user.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "my thread",
        "preset-a",
        "generic_chat",
        "",
      ),
    );
  });

  it("generic_chat 可带 cwd 创建 workspace thread", async () => {
    createThread.mockResolvedValue({
      id: "thread-cwd",
      name: "workspace run",
      preset_id: "preset-a",
      backend_kind: "generic_chat",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
      cwd: "/tmp/workspace",
    });
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "workspace run");
    await user.type(screen.getByLabelText("cwd"), "/tmp/workspace");
    await user.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "workspace run",
        "preset-a",
        "generic_chat",
        "/tmp/workspace",
      ),
    );
  });

  it("浏览器环境点击文件夹按钮会走可测试降级提示", async () => {
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "打开工作目录选择器" }));

    await waitFor(() =>
      expect(dirPicker.pickDirectory).toHaveBeenCalledWith({
        title: "选择工作目录",
        defaultPath: undefined,
      }),
    );
    expect(toast.error).toHaveBeenCalledWith("系统文件管理器入口需要 Tauri 环境");
  });

  it("Tauri 选择工作目录后回填 cwd 并随创建提交", async () => {
    vi.mocked(dirPicker.isTauriEnv).mockReturnValue(true);
    vi.mocked(dirPicker.pickDirectory).mockResolvedValue("E:\\xgt\\proj\\app");
    createThread.mockResolvedValue({
      id: "thread-picked",
      name: "picked",
      preset_id: "preset-a",
      backend_kind: "generic_chat",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
      cwd: "E:\\xgt\\proj\\app",
    });

    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "picked");
    await user.click(screen.getByRole("button", { name: "打开工作目录选择器" }));

    await waitFor(() =>
      expect(screen.getByLabelText("cwd")).toHaveValue("E:\\xgt\\proj\\app"),
    );
    await user.click(screen.getByRole("button", { name: /创建/ }));

    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "picked",
        "preset-a",
        "generic_chat",
        "E:\\xgt\\proj\\app",
      ),
    );
  });

  it("空 name → 创建按钮可点击（v0.1.5 后 name 可选，留空使用服务器 ID 兜底）", () => {
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: /创建/ })).not.toBeDisabled();
  });

  it("选 claude_code 时 preset select 隐藏，提交带空 preset", async () => {
    createThread.mockResolvedValue({
      id: "thread-claude",
      name: "claude",
      preset_id: "",
      backend_kind: "claude_code",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
    });
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "claude run");
    // 切 backend 到 claude_code
    const backendSel = screen.getByLabelText("backend") as HTMLSelectElement;
    await user.selectOptions(backendSel, "claude_code");
    // preset select 应消失
    expect(screen.queryByLabelText("preset")).toBeNull();
    await user.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "claude run",
        "",
        "claude_code",
        "",
      ),
    );
  });

  it("backend=claude_code 时即使没有 preset 也能创建", async () => {
    // 清空 presets 模拟 fresh 环境
    useThreadsStore.setState({
      threads: [],
      presets: [],
      loading: false,
      createThread,
      renameThread: vi.fn(),
      deleteThread: vi.fn(),
      fetchThreads: vi.fn(),
      fetchPresets: vi.fn(),
    });
    createThread.mockResolvedValue({
      id: "thread-claude",
      name: "c",
      preset_id: "",
      backend_kind: "claude_code",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
    });
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "c");
    const backendSel = screen.getByLabelText("backend") as HTMLSelectElement;
    await user.selectOptions(backendSel, "claude_code");
    const btn = screen.getByRole("button", { name: /创建/ });
    expect(btn).not.toBeDisabled();
    await user.click(btn);
    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith("c", "", "claude_code", ""),
    );
  });

  it("相对 cwd 会禁用创建", async () => {
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByPlaceholderText(/不填则使用服务器ID/), "bad cwd");
    await user.type(screen.getByLabelText("cwd"), "relative/path");
    expect(screen.getByRole("button", { name: /创建/ })).toBeDisabled();
    expect(screen.getByText(/工作目录需要绝对路径/)).toBeInTheDocument();
  });
});
