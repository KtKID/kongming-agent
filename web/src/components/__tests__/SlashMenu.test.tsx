import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer } from "@/components/Composer";
import { SlashMenu, type SlashMenuEntry } from "@/components/SlashMenu";
import { fetchSlashCatalogItems } from "@/lib/slash-catalog";

vi.mock("@/lib/slash-catalog", () => ({
  fetchSlashCatalogItems: vi.fn(),
}));

const workflowItem = {
  id: "workflow:fake",
  group_id: "workflow",
  kind: "workflow_strategy" as const,
  title: "Fake Workflow",
  description: "Fake workflow summary",
  source_ref: "workflow_strategy:fake",
  order: 0,
  section_id: "registered",
  slash: null,
  insert_text: "/workflow fake ",
  action: "bind_reference" as const,
  reference_template: {
    kind: "workflow_strategy" as const,
    ref: "workflow_strategy:fake",
    label: "Fake Workflow",
    activation: "start_workflow" as const,
    source_ref: "workflow_strategy:fake",
    args: { mode: "fake" },
    metadata: { mode: "fake", runnable: true },
  },
  enabled: true,
  metadata: { mode: "fake" },
  diagnostics: [],
};

const workflowRunItem = {
  ...workflowItem,
  id: "workflow-run:run-1",
  kind: "workflow_run" as const,
  title: "Recent Evolution Review",
  description: "Completed workflow",
  source_ref: "workflow_run:thread-1:run-1",
  order: 1000,
  section_id: "completed",
  insert_text: "/workflow-run run-1 ",
  action: "open_viewer" as const,
  reference_template: {
    kind: "workflow_run" as const,
    ref: "workflow_run:run-1",
    label: "Recent Evolution Review",
    activation: "open_viewer" as const,
    source_ref: "workflow_run:thread-1:run-1",
    args: { thread_id: "thread-1", workflow_id: "run-1" },
    metadata: { mode: "fake", status: "completed" },
  },
  metadata: { workflow_id: "run-1", mode: "fake", status: "completed" },
};

const evolveItem = {
  id: "command:evolve",
  group_id: "command",
  kind: "command" as const,
  title: "进化复盘",
  description: "触发一次独立进化复盘",
  source_ref: "command:evolve",
  order: 0,
  section_id: "system",
  slash: "/evolve",
  insert_text: "/evolve ",
  action: "insert_text" as const,
  reference_template: null,
  enabled: true,
  metadata: {
    executor_key: "evolution_review",
    accepts_args: false,
  },
  diagnostics: [],
};

const skillItem = {
  id: "skill:skill-creator",
  group_id: "skill",
  kind: "skill" as const,
  title: "Skill Creator",
  description: "Create and improve skills",
  source_ref: "skill:skill-creator",
  order: 0,
  section_id: null,
  slash: "/skill-creator",
  insert_text: "/skill-creator ",
  action: "bind_reference" as const,
  reference_template: {
    kind: "skill" as const,
    ref: "skill:skill-creator",
    label: "Skill Creator",
    activation: "inject_context" as const,
    source_ref: "E:/xgt/proj/AgentSetting/skills/skill-creator/SKILL.md",
    args: {},
    metadata: { root: "r1" },
  },
  enabled: true,
  metadata: { root: "r1" },
  diagnostics: [],
};

afterEach(() => {
  vi.resetAllMocks();
});

function mockCatalog() {
  vi.mocked(fetchSlashCatalogItems).mockResolvedValue([
    workflowItem,
    evolveItem,
    skillItem,
  ]);
}

describe("SlashMenu catalog flow", () => {
  it("scrolls the active entry into view when keyboard selection moves", async () => {
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    const entries: SlashMenuEntry[] = Array.from({ length: 12 }, (_, index) => ({
      type: "item" as const,
      id: `item:${index}`,
      item: {
        ...skillItem,
        id: `skill:${index}`,
        title: `Skill ${index}`,
        description: `Skill ${index} description`,
      },
    }));

    const { rerender } = render(
      <SlashMenu
        entries={entries}
        onActivate={vi.fn()}
        onClose={vi.fn()}
        visible
        activeIndex={0}
      />,
    );
    await waitFor(() =>
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" }),
    );
    scrollIntoView.mockClear();

    rerender(
      <SlashMenu
        entries={entries}
        onActivate={vi.fn()}
        onClose={vi.fn()}
        visible
        activeIndex={9}
      />,
    );

    await waitFor(() =>
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" }),
    );
  });

  it("opens a flat catalog and binds a workflow item without group navigation", async () => {
    mockCatalog();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} threadId="thread-1" />);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/");
    expect(await screen.findByText("Fake Workflow")).toBeInTheDocument();
    expect(screen.getByText("/evolve")).toBeInTheDocument();
    expect(screen.getByText("/skill-creator")).toBeInTheDocument();
    expect(screen.getByText("Workflow")).toBeInTheDocument();
    expect(screen.getByText("Command")).toBeInTheDocument();
    expect(screen.getByText("Skill")).toBeInTheDocument();
    await user.click(screen.getByText("Fake Workflow"));

    expect(screen.getByTestId("composer-reference-chip")).toHaveTextContent(
      "Fake Workflow",
    );
    await waitFor(() => expect(input).toHaveValue(""));
    await user.click(screen.getByRole("button", { name: /发送/ }));
    expect(onSubmit).toHaveBeenCalledWith(
      "必须使用 fake workflow 完成用户需求或任务",
      null,
      undefined,
      undefined,
      {
        text: "",
        reasoningEffort: null,
        attachments: [],
        references: [
          expect.objectContaining({
            ...workflowItem.reference_template,
            metadata: expect.objectContaining(
              workflowItem.reference_template.metadata,
            ),
          }),
        ],
      },
    );
    expect(fetchSlashCatalogItems).toHaveBeenCalledWith("thread-1");
  });

  it("labels completed workflow runs as Workflow", async () => {
    vi.mocked(fetchSlashCatalogItems).mockResolvedValue([workflowRunItem]);
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/recent");

    const item = await screen.findByText("Recent Evolution Review");
    expect(item.closest("button")).toHaveTextContent("Workflow");
    expect(item.closest("button")).not.toHaveTextContent("History");
  });

  it("finds /evolve directly from /evo and inserts the control command", async () => {
    mockCatalog();
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/evo");

    expect(await screen.findByText("/evolve")).toBeInTheDocument();
    expect(screen.getByText("触发一次独立进化复盘")).toBeInTheDocument();
    expect(screen.getByText("Command")).toBeInTheDocument();
    await user.click(screen.getByText("/evolve"));

    await waitFor(() => expect(input).toHaveValue("/evolve "));
  });

  it("finds /evolve through ordered-character fuzzy matching", async () => {
    mockCatalog();
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/evl");

    expect(await screen.findByText("/evolve")).toBeInTheDocument();
    expect(screen.getByText("Command")).toBeInTheDocument();
  });

  it("keeps ordered-character fuzzy matching out of descriptions", async () => {
    vi.mocked(fetchSlashCatalogItems).mockResolvedValue([
      {
        ...workflowItem,
        description: "Ask a child reviewer to inspect the workflow",
      },
    ]);
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/evo");

    expect(await screen.findByText("No matching actions")).toBeInTheDocument();
    expect(screen.queryByText("Fake Workflow")).not.toBeInTheDocument();
  });

  it("uses command priority when command and skill have the same search score", async () => {
    vi.mocked(fetchSlashCatalogItems).mockResolvedValue([
      {
        ...skillItem,
        id: "skill:review",
        title: "Review skill",
        slash: "/review",
      },
      {
        ...evolveItem,
        id: "command:review",
        title: "Review command",
        slash: "/review",
      },
    ]);
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/review");

    const matches = await screen.findAllByText("/review");
    expect(matches[0].closest("button")).toHaveTextContent("Command");
    expect(matches[1].closest("button")).toHaveTextContent("Skill");
  });

  it("shows an empty result state for an unmatched query", async () => {
    vi.mocked(fetchSlashCatalogItems).mockResolvedValue([]);
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/missing");

    expect(await screen.findByText("No matching actions")).toBeInTheDocument();
  });

  it("renders unavailable catalog items as disabled actions", async () => {
    vi.mocked(fetchSlashCatalogItems).mockResolvedValue([
      { ...evolveItem, enabled: false },
    ]);
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/evo");

    const action = (await screen.findByText("/evolve")).closest("button");
    expect(action).toBeDisabled();
    await user.click(action!);
    expect(input).toHaveValue("/evo");
  });

  it("hides the previous thread catalog immediately when threadId changes", async () => {
    vi.mocked(fetchSlashCatalogItems)
      .mockResolvedValueOnce([evolveItem])
      .mockReturnValueOnce(new Promise(() => undefined));
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer onSubmit={onSubmit} threadId="thread-a" />,
    );
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/evo");
    expect(await screen.findByText("/evolve")).toBeInTheDocument();

    rerender(<Composer onSubmit={onSubmit} threadId="thread-b" />);

    expect(screen.queryByTestId("slash-menu")).not.toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("shows a loading state while the flat catalog is resolving", async () => {
    vi.mocked(fetchSlashCatalogItems).mockReturnValue(
      new Promise(() => undefined),
    );
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/");

    expect(await screen.findByText("Loading")).toBeInTheDocument();
  });

  it("selects the active result with Enter and closes the menu with Escape", async () => {
    mockCatalog();
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/evo");
    expect(await screen.findByText("/evolve")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(input).toHaveValue("/evolve "));

    await user.clear(input);
    await user.type(input, "/evo");
    expect(await screen.findByText("/evolve")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("slash-menu")).not.toBeInTheDocument();
  });

  it("shows loading failure state when group loading fails", async () => {
    vi.mocked(fetchSlashCatalogItems).mockRejectedValue(new Error("boom"));
    render(<Composer onSubmit={vi.fn()} threadId="thread-1" />);
    const user = userEvent.setup();

    await user.type(screen.getByRole("textbox"), "/");

    expect(await screen.findByText("Load failed")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("binds selected skill as a chip and submits expanded skill link text", async () => {
    mockCatalog();
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} threadId="thread-1" />);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox");

    await user.type(input, "/skill");
    await user.click(await screen.findByText("/skill-creator"));

    expect(screen.getByTestId("composer-reference-chip")).toHaveTextContent(
      "Skill Creator",
    );
    await waitFor(() => expect(input).toHaveValue(""));

    await user.click(screen.getByRole("button", { name: /发送/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      "[$skill-creator](E:/xgt/proj/AgentSetting/skills/skill-creator/SKILL.md)",
      null,
      undefined,
      undefined,
      {
        text: "",
        reasoningEffort: null,
        attachments: [],
        references: [
          expect.objectContaining({
            ...skillItem.reference_template,
            metadata: expect.objectContaining(skillItem.reference_template.metadata),
          }),
        ],
      },
    );
  });
});
