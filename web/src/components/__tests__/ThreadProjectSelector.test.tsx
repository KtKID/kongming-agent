import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ThreadProjectSelector,
  deriveThreadProjectOptions,
  type ThreadProjectOption,
} from "@/components/ThreadProjectSelector";
import type { ThreadMetadataDTO } from "@/protocol";

const pickerMock = vi.hoisted(() => ({
  pickDirectory: vi.fn(),
}));

vi.mock("@/lib/dirPicker", () => ({
  pickDirectory: pickerMock.pickDirectory,
}));

function makeThread(overrides: Partial<ThreadMetadataDTO> = {}): ThreadMetadataDTO {
  return {
    id: "thread-111111111111",
    name: "thread",
    preset_id: "preset-a",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "",
    created_at: 100,
    updated_at: 100,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    schema_version: 10,
    ...overrides,
  };
}

describe("deriveThreadProjectOptions", () => {
  it("过滤空 cwd、相对路径，并按规范化 cwd 去重", () => {
    const options = deriveThreadProjectOptions([
      makeThread({ id: "thread-aaaaaaaaaaaa", cwd: "", updated_at: 10 }),
      makeThread({ id: "thread-bbbbbbbbbbbb", cwd: "relative/project", updated_at: 20 }),
      makeThread({ id: "thread-cccccccccccc", cwd: "/tmp/project-a", updated_at: 30 }),
      makeThread({ id: "thread-dddddddddddd", cwd: "/tmp/project-a/", updated_at: 40 }),
      makeThread({ id: "thread-eeeeeeeeeeee", cwd: "/tmp/project-b", updated_at: 50 }),
    ]);

    expect(options.map((option) => option.cwd)).toEqual([
      "/tmp/project-b",
      "/tmp/project-a",
    ]);
    expect(options.find((option) => option.cwd === "/tmp/project-a")?.threadCount).toBe(2);
  });

  it("Windows 盘符路径按大小写和分隔符规范化去重", () => {
    const options = deriveThreadProjectOptions([
      makeThread({ id: "thread-aaaaaaaaaaaa", cwd: "C:\\Repo\\App", updated_at: 10 }),
      makeThread({ id: "thread-bbbbbbbbbbbb", cwd: "c:/Repo/App/", updated_at: 20 }),
    ]);

    expect(options).toHaveLength(1);
    expect(options[0]).toMatchObject({
      cwd: "C:\\Repo\\App",
      label: "App",
      threadCount: 2,
    });
  });
});

describe("ThreadProjectSelector", () => {
  beforeEach(() => {
    pickerMock.pickDirectory.mockReset();
  });

  it("选择新项目后返回文件夹 cwd", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    pickerMock.pickDirectory.mockResolvedValue("/tmp/new-project");

    render(
      <ThreadProjectSelector
        threads={[]}
        value={null}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByTestId("thread-project-selector-trigger"));
    await user.click(screen.getByTestId("thread-project-pick-directory"));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({
        cwd: "/tmp/new-project",
        label: "new-project",
        title: "/tmp/new-project",
        threadCount: 0,
        source: "file_picker",
      } satisfies ThreadProjectOption),
    );
  });

  it("选择不需要项目后返回空 cwd", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();

    render(
      <ThreadProjectSelector
        threads={[makeThread({ cwd: "/tmp/project-a" })]}
        value={null}
        onChange={onChange}
      />,
    );

    await user.click(screen.getByTestId("thread-project-selector-trigger"));
    await user.click(screen.getByTestId("thread-project-none"));

    expect(onChange).toHaveBeenCalledWith({
      cwd: "",
      label: "不需要项目",
      title: "不绑定项目目录",
      threadCount: 0,
      source: "none",
    } satisfies ThreadProjectOption);
  });
});
