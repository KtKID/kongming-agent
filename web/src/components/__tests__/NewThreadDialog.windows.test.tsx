import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

describe("NewThreadDialog Windows cwd", () => {
  it("accepts drive-letter absolute paths", async () => {
    createThread.mockResolvedValue({
      id: "thread-win",
      name: "windows run",
      preset_id: "preset-a",
      backend_kind: "generic_chat",
      created_at: 1,
      updated_at: 1,
      message_count: 0,
      usage_summary: null,
      cwd: "E:\\xgt\\proj\\agent-proj\\kongming-agent",
    });

    render(
      <MemoryRouter>
        <NewThreadDialog open={true} onOpenChange={vi.fn()} />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.type(screen.getAllByRole("textbox")[0], "windows run");
    await user.type(
      screen.getByLabelText("cwd"),
      "E:\\xgt\\proj\\agent-proj\\kongming-agent",
    );
    await user.click(screen.getByRole("button", { name: /创建/ }));

    await waitFor(() =>
      expect(createThread).toHaveBeenCalledWith(
        "windows run",
        "preset-a",
        "generic_chat",
        "E:\\xgt\\proj\\agent-proj\\kongming-agent",
      ),
    );
  });
});
