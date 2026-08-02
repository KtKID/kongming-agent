import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GenericEmptyThreadView } from "@/components/GenericEmptyThreadView";
import { useModelProvidersStore } from "@/modules/model-providers/store";
import type { ConnectedModelFamily } from "@/modules/model-providers/types";
import type { ThreadMetadataDTO } from "@/protocol";
import { useThreadsStore } from "@/stores/threads";

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: toastMock,
}));

function makeThread(overrides: Partial<ThreadMetadataDTO> = {}): ThreadMetadataDTO {
  return {
    id: "thread-aaaaaaaaaaaa",
    name: "thread",
    preset_id: "preset-a",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "/tmp/project-a",
    created_at: 100,
    updated_at: 100,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    schema_version: 10,
    ...overrides,
  };
}

const modelFamilies: ConnectedModelFamily[] = [
  {
    providerId: "local",
    providerLabel: "Local",
    familyId: "local:test-model",
    displayName: "Test Model",
    presetId: "preset-a",
    model: "test-model",
    connected: true,
    supportedReasoningEfforts: ["none", "high", "max"],
    defaultReasoningEffort: "high",
    reasoningAdapter: "deepseek_anthropic_thinking",
    contextWindowTokens: 131072,
  },
];

beforeEach(() => {
  toastMock.error.mockReset();
  useThreadsStore.setState({
    threads: [],
    pendingNewSession: {
      backendKind: "generic_chat",
      cwd: "",
      projectName: "",
    },
    createGenericThreadFromFirstMessage: vi.fn(),
  } as unknown as Parameters<typeof useThreadsStore.setState>[0]);
  useModelProvidersStore.setState({
    modelFamilies,
    loadModelFamilies: vi.fn().mockResolvedValue(undefined),
  } as unknown as Parameters<typeof useModelProvidersStore.setState>[0]);
});

describe("GenericEmptyThreadView", () => {
  it("发送首条消息成功后调用首发接口并回调真实 thread", async () => {
    const user = userEvent.setup();
    const created = makeThread({ message_count: 1 });
    const createGenericThreadFromFirstMessage = vi.fn().mockResolvedValue(created);
    const onCreated = vi.fn();
    useThreadsStore.setState({
      createGenericThreadFromFirstMessage,
    } as unknown as Parameters<typeof useThreadsStore.setState>[0]);

    render(<GenericEmptyThreadView onCreated={onCreated} />);

    await waitFor(() =>
      expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent("Test Model"),
    );
    await user.type(screen.getByLabelText("消息输入"), "hello generic");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    await waitFor(() =>
      expect(createGenericThreadFromFirstMessage).toHaveBeenCalledWith({
        text: "hello generic",
        preset_id: "preset-a",
        cwd: "",
        reasoning_effort: "high",
      }),
    );
    expect(onCreated).toHaveBeenCalledWith(created, "high");
  });

  it("首发显式关闭后把 none 连同真实 thread 交给上层", async () => {
    const user = userEvent.setup();
    const created = makeThread({ message_count: 1 });
    const createGenericThreadFromFirstMessage = vi.fn().mockResolvedValue(created);
    const onCreated = vi.fn();
    useThreadsStore.setState({
      createGenericThreadFromFirstMessage,
    } as unknown as Parameters<typeof useThreadsStore.setState>[0]);

    render(<GenericEmptyThreadView onCreated={onCreated} />);

    await waitFor(() =>
      expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent("Test Model"),
    );
    await user.click(screen.getByRole("button", { name: /深度思考/ }));
    await user.click(screen.getByRole("menuitemradio", { name: "关闭" }));
    await user.type(screen.getByLabelText("消息输入"), "disable reasoning");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    await waitFor(() =>
      expect(createGenericThreadFromFirstMessage).toHaveBeenCalledWith({
        text: "disable reasoning",
        preset_id: "preset-a",
        cwd: "",
        reasoning_effort: "none",
      }),
    );
    expect(onCreated).toHaveBeenCalledWith(created, "none");
  });

  it("创建失败时保留输入内容和项目选择", async () => {
    const user = userEvent.setup();
    const createGenericThreadFromFirstMessage = vi
      .fn()
      .mockRejectedValue(new Error("boom"));
    useThreadsStore.setState({
      threads: [makeThread({ cwd: "/tmp/project-a" })],
      createGenericThreadFromFirstMessage,
    } as unknown as Parameters<typeof useThreadsStore.setState>[0]);

    render(<GenericEmptyThreadView onCreated={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent("Test Model"),
    );
    await user.click(screen.getByTestId("thread-project-selector-trigger"));
    await user.click(screen.getByText("project-a"));
    await user.type(screen.getByLabelText("消息输入"), "keep this");
    await user.click(screen.getByRole("button", { name: /发送/ }));

    await waitFor(() =>
      expect(toastMock.error).toHaveBeenCalledWith("创建会话失败：boom"),
    );
    expect(screen.getByLabelText("消息输入")).toHaveValue("keep this");
    expect(screen.getByTestId("thread-project-selector-trigger")).toHaveTextContent(
      "project-a",
    );
  });

  it("Composer 只展示当前模型支持的思考档位", async () => {
    const user = userEvent.setup();

    render(<GenericEmptyThreadView onCreated={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByTestId("composer-model-switcher")).toHaveTextContent("Test Model"),
    );

    await user.click(screen.getByRole("button", { name: /深度思考/ }));

    expect(screen.getByRole("menuitemradio", { name: "关闭" })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: "高" })).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: "最高" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitemradio", { name: "低" })).toBeNull();
    expect(screen.queryByRole("menuitemradio", { name: "中" })).toBeNull();
  });
});
