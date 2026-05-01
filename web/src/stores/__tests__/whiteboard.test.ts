import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWhiteboardStore } from "@/stores/whiteboard";

const apiMocks = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiGet: apiMocks.apiGet,
  apiPost: apiMocks.apiPost,
  apiPut: apiMocks.apiPut,
  apiPatch: apiMocks.apiPatch,
  apiDelete: apiMocks.apiDelete,
}));

function makeBoard() {
  return {
    title: "白板",
    schema_version: 1,
    cards: [
      {
        id: "card-1",
        title: "发版前检查",
        category: "todo",
        filename: "card-todo-a1b2.md",
        content: "- [ ] step 1",
        x: 24,
        y: 32,
        height: 260,
        collapsed: false,
        z_index: 1,
        updated_at: 1100,
      },
    ],
  };
}

describe("stores/whiteboard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    apiMocks.apiGet.mockReset();
    apiMocks.apiPost.mockReset();
    apiMocks.apiPut.mockReset();
    apiMocks.apiPatch.mockReset();
    apiMocks.apiDelete.mockReset();
    useWhiteboardStore.setState({
      boardTitle: "白板",
      boardUpdatedAt: 0,
      cards: [],
      loading: false,
      selectedCardId: null,
      draggingCardId: null,
      resizingCardId: null,
    });
  });

  it("fetchBoard 拉取白板快照", async () => {
    apiMocks.apiGet.mockResolvedValue(makeBoard());
    await useWhiteboardStore.getState().fetchBoard();
    const state = useWhiteboardStore.getState();
    expect(state.boardTitle).toBe("白板");
    expect(state.cards).toHaveLength(1);
    expect(state.cards[0]!.title).toBe("发版前检查");
  });

  it("createCard 用返回快照回填 store", async () => {
    const board = makeBoard();
    const card = {
      id: "card-2",
      title: "新卡片",
      category: "note",
      filename: "card-note-c3d4.md",
      content: "hello",
      x: 48,
      y: 56,
      height: 240,
      collapsed: false,
      z_index: 2,
      updated_at: 1200,
    };
    useWhiteboardStore.setState({
      boardTitle: "白板",
      cards: board.cards.map((item) => ({
        id: item.id,
        title: item.title,
        category: item.category,
        content: item.content,
        fileName: item.filename,
        x: item.x,
        y: item.y,
        width: 320,
        height: item.height,
        collapsed: item.collapsed,
        zIndex: item.z_index,
        dirtyContent: false,
        dirtyLayout: false,
        saving: false,
        updatedAt: item.updated_at,
      })),
    });
    apiMocks.apiPost.mockResolvedValue(card);
    await useWhiteboardStore.getState().createCard({
      title: "新卡片",
      category: "note",
      content: "hello",
    });
    expect(useWhiteboardStore.getState().cards).toHaveLength(2);
  });

  it("updateCardContentLocal 标记 dirty 并 debounce 保存", async () => {
    useWhiteboardStore.setState({
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          content: "old",
          fileName: "a.md",
          x: 0,
          y: 0,
          width: 320,
          height: 200,
          collapsed: false,
          zIndex: 1,
          dirtyContent: false,
          dirtyLayout: false,
          saving: false,
          updatedAt: 10,
        },
      ],
    });
    apiMocks.apiPut.mockResolvedValue({
      id: "card-1",
      title: "A",
      category: "note",
      filename: "a.md",
      content: "new",
      x: 0,
      y: 0,
      height: 200,
      collapsed: false,
      z_index: 1,
      updated_at: 20,
    });

    useWhiteboardStore.getState().updateCardContentLocal("card-1", "new");
    expect(useWhiteboardStore.getState().cards[0]!.dirtyContent).toBe(true);

    await vi.advanceTimersByTimeAsync(500);
    expect(apiMocks.apiPut).toHaveBeenCalledOnce();
    expect(useWhiteboardStore.getState().cards[0]!.content).toBe("new");
  });

  it("saveLayout 节流后提交布局快照", async () => {
    useWhiteboardStore.setState({
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          content: "x",
          fileName: "a.md",
          x: 0,
          y: 0,
          width: 320,
          height: 200,
          collapsed: false,
          zIndex: 1,
          dirtyContent: false,
          dirtyLayout: false,
          saving: false,
          updatedAt: 10,
        },
      ],
    });
    apiMocks.apiPatch.mockResolvedValue({
      title: "白板",
      schema_version: 1,
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          filename: "a.md",
          content: "x",
          x: 10,
          y: 20,
          height: 200,
          collapsed: false,
          z_index: 1,
          updated_at: 10,
        },
      ],
    });

    useWhiteboardStore.getState().updateCardLayoutLocal("card-1", {
      x: 10,
      y: 20,
    });
    await vi.advanceTimersByTimeAsync(300);

    expect(apiMocks.apiPatch).toHaveBeenCalledOnce();
    expect(useWhiteboardStore.getState().cards[0]!.x).toBe(10);
  });

  it("旧的内容保存响应不会覆盖更新后的本地编辑", async () => {
    let resolveFirstSave!: (value: unknown) => void;
    apiMocks.apiPut.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirstSave = resolve;
        }),
    );
    apiMocks.apiPut.mockResolvedValueOnce({
      id: "card-1",
      title: "A",
      category: "note",
      filename: "a.md",
      content: "newer",
      x: 0,
      y: 0,
      height: 200,
      collapsed: false,
      z_index: 1,
      updated_at: 21,
    });
    useWhiteboardStore.setState({
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          content: "old",
          fileName: "a.md",
          x: 0,
          y: 0,
          width: 320,
          height: 200,
          collapsed: false,
          zIndex: 1,
          dirtyContent: false,
          dirtyLayout: false,
          saving: false,
          updatedAt: 10,
          contentRevision: 0,
          layoutRevision: 0,
        },
      ],
    });

    useWhiteboardStore.getState().updateCardContentLocal("card-1", "new");
    await vi.advanceTimersByTimeAsync(500);
    useWhiteboardStore.getState().updateCardContentLocal("card-1", "newer");

    resolveFirstSave({
      id: "card-1",
      title: "A",
      category: "note",
      filename: "a.md",
      content: "new",
      x: 0,
      y: 0,
      height: 200,
      collapsed: false,
      z_index: 1,
      updated_at: 20,
    });
    await Promise.resolve();

    const intermediate = useWhiteboardStore.getState().cards[0]!;
    expect(intermediate.content).toBe("newer");
    expect(intermediate.dirtyContent).toBe(true);
    expect(intermediate.updatedAt).toBe(20);

    await vi.advanceTimersByTimeAsync(500);
    expect(apiMocks.apiPut).toHaveBeenCalledTimes(2);
    expect(apiMocks.apiPut.mock.calls[1]?.[1]).toMatchObject({
      content: "newer",
      expected_updated_at: 20,
    });
  });

  it("旧的布局保存响应不会回滚更新后的本地布局", async () => {
    let resolveFirstLayout!: (value: unknown) => void;
    apiMocks.apiPatch.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirstLayout = resolve;
        }),
    );
    apiMocks.apiPatch.mockResolvedValueOnce({
      title: "白板",
      schema_version: 1,
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          filename: "a.md",
          content: "x",
          x: 20,
          y: 30,
          height: 200,
          collapsed: false,
          z_index: 1,
          updated_at: 10,
        },
      ],
    });
    useWhiteboardStore.setState({
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          content: "x",
          fileName: "a.md",
          x: 0,
          y: 0,
          width: 320,
          height: 200,
          collapsed: false,
          zIndex: 1,
          dirtyContent: false,
          dirtyLayout: false,
          saving: false,
          updatedAt: 10,
          contentRevision: 0,
          layoutRevision: 0,
        },
      ],
    });

    useWhiteboardStore.getState().updateCardLayoutLocal("card-1", { x: 10, y: 10 });
    await vi.advanceTimersByTimeAsync(300);
    useWhiteboardStore.getState().updateCardLayoutLocal("card-1", { x: 20, y: 30 });

    resolveFirstLayout({
      title: "白板",
      schema_version: 1,
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          filename: "a.md",
          content: "x",
          x: 10,
          y: 10,
          height: 200,
          collapsed: false,
          z_index: 1,
          updated_at: 10,
        },
      ],
    });
    await Promise.resolve();

    const intermediate = useWhiteboardStore.getState().cards[0]!;
    expect(intermediate.x).toBe(20);
    expect(intermediate.y).toBe(30);
    expect(intermediate.dirtyLayout).toBe(true);

    await vi.advanceTimersByTimeAsync(300);
    expect(apiMocks.apiPatch).toHaveBeenCalledTimes(2);
    expect(apiMocks.apiPatch.mock.calls[1]?.[1]).toMatchObject({
      cards: [
        expect.objectContaining({
          id: "card-1",
          x: 20,
          y: 30,
        }),
      ],
    });
  });

  it("deleteCard 删除本地卡片", async () => {
    useWhiteboardStore.setState({
      cards: [
        {
          id: "card-1",
          title: "A",
          category: "note",
          content: "x",
          fileName: "a.md",
          x: 0,
          y: 0,
          width: 320,
          height: 200,
          collapsed: false,
          zIndex: 1,
          dirtyContent: false,
          dirtyLayout: false,
          saving: false,
          updatedAt: 10,
        },
      ],
    });
    apiMocks.apiDelete.mockResolvedValue(undefined);
    await useWhiteboardStore.getState().deleteCard("card-1");
    expect(useWhiteboardStore.getState().cards).toHaveLength(0);
  });
});
