import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { NewThreadDialog } from "@/components/NewThreadDialog";
import { useThreadsStore } from "@/stores/threads";

const createThread = vi.fn();

beforeEach(() => {
  createThread.mockReset();
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
  it("提交触发 createThread(name, presetId)", async () => {
    createThread.mockResolvedValue({
      id: "thread-xyz",
      name: "test",
      preset_id: "preset-a",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
    });
    const onOpenChange = vi.fn();
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={onOpenChange} />
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(
      screen.getByPlaceholderText(/会话名/),
      "my thread",
    );
    await user.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith("my thread", "preset-a"),
    );
  });

  it("空 name → 创建按钮禁用", () => {
    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: /创建/ })).toBeDisabled();
  });
});
