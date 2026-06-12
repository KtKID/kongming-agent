import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/lib/api";
import type {
  CardScope,
  CreateWhiteboardCardRequest,
  WhiteboardCardDTO,
  UpdateWhiteboardCardRequest,
  UpdateWhiteboardLayoutRequest,
  WhiteboardDTO,
} from "@/protocol";

/**
 * 单张白板卡片在前端 store 内的形态。
 *
 * 与 `WhiteboardCardDTO` 的主要差别：
 * - 加 `scope`（v0.2 whiteboard-scope 协议必填，决定 layout 走哪条 PATCH 路径）
 * - 加 `dirtyContent` / `dirtyLayout` / `saving` / `error` 等本地状态
 * - 加 `contentRevision` / `layoutRevision` 用于 debounce 期间的乐观更新冲突检测
 */
export interface WhiteboardCardState {
  scope: CardScope;
  id: string;
  title: string;
  category: string;
  content: string;
  fileName: string;
  x: number;
  y: number;
  width: number;
  height: number;
  collapsed: boolean;
  zIndex: number;
  dirtyContent: boolean;
  dirtyLayout: boolean;
  saving: boolean;
  error?: string;
  updatedAt: number;
  contentRevision?: number;
  layoutRevision?: number;
}

interface WhiteboardState {
  /** 当前白板绑定的 thread.id；切 thread 时由 `fetchBoard(threadId)` 同步设置。 */
  currentThreadId: string | null;
  /** global board 的 title（默认 "Whiteboard"，由 `<root>/global/board.json` 决定）。 */
  globalTitle: string;
  /** 当前 thread.cwd 对应的 project board 的 title；cwd 为空或项目目录不存在时为 null。 */
  projectTitle: string | null;
  boardUpdatedAt: number;
  cards: WhiteboardCardState[];
  loading: boolean;
  selectedCardId: string | null;
  draggingCardId: string | null;
  resizingCardId: string | null;
  /**
   * 拉取指定 thread 的白板（global + project 合并视图）。
   *
   * 同步设置 `currentThreadId`，之后 `createCard` / `saveCardContent`
   * / `saveLayout` / `deleteCard` 都用这个 threadId 拼 URL。
   */
  fetchBoard: (threadId: string) => Promise<void>;
  /**
   * 清空白板（用于 threadId=null 的纯聊天 tab）。
   *
   * 复位 currentThreadId / cards / titles，但不发请求。
   */
  clearBoard: () => void;
  createCard: (input: {
    scope: CardScope;
    title: string;
    category: string;
    content?: string;
    x?: number;
    y?: number;
    height?: number;
    collapsed?: boolean;
  }) => Promise<void>;
  updateCardContentLocal: (cardId: string, content: string) => void;
  saveCardContent: (cardId: string) => Promise<void>;
  updateCardMetaLocal: (
    cardId: string,
    patch: Partial<Pick<WhiteboardCardState, "title" | "category">>,
  ) => void;
  updateCardLayoutLocal: (
    cardId: string,
    patch: Partial<
      Pick<
        WhiteboardCardState,
        "x" | "y" | "width" | "height" | "collapsed" | "zIndex"
      >
    >,
  ) => void;
  /**
   * 保存指定 scope 的 dirty layout。
   *
   * scope 由调用方传入：拖拽事件本来就知道是在 global 还是 project 卡上发生的；
   * 让 store 反查 scope 反而要遍历两份 board.json。
   *
   * 内部行为：从 `state.cards` 里选出 `card.scope === scope && card.dirtyLayout`
   * 的卡片，整体 PATCH（layout 是该 scope 的全量布局，后端按 id 匹配更新）。
   */
  saveLayout: (scope: CardScope) => Promise<void>;
  deleteCard: (cardId: string) => Promise<void>;
  setSelectedCard: (cardId: string | null) => void;
  setDraggingCard: (cardId: string | null) => void;
  setResizingCard: (cardId: string | null) => void;
  bringCardToFront: (cardId: string) => void;
  toggleCollapsed: (cardId: string) => void;
}

const CONTENT_SAVE_DELAY_MS = 500;
const LAYOUT_SAVE_DELAY_MS = 300;

const contentTimers = new Map<string, number>();
/**
 * layout debounce timer 按 scope 分桶：global 和 project 各一条独立的 debounce 计时。
 *
 * 用户连续拖拽 global 卡 → 只触发 global PATCH；
 * 跨 scope 同时拖（极少见）→ 各自独立 debounce 触发，互不影响。
 */
const layoutSaveTimers = new Map<CardScope, number>();

function clearContentTimer(cardId: string): void {
  const timer = contentTimers.get(cardId);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    contentTimers.delete(cardId);
  }
}

function clearLayoutTimer(scope: CardScope): void {
  const timer = layoutSaveTimers.get(scope);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    layoutSaveTimers.delete(scope);
  }
}

function mapBoard(dto: WhiteboardDTO): WhiteboardCardState[] {
  return dto.cards
    .slice()
    .sort((a, b) => a.z_index - b.z_index)
    .map(mapCard);
}

function mapCard(card: WhiteboardCardDTO): WhiteboardCardState {
  return {
    scope: card.scope,
    id: card.id,
    title: card.title,
    category: card.category,
    content: card.content,
    fileName: card.filename,
    x: card.x,
    y: card.y,
    width: 320,
    height: card.height,
    collapsed: card.collapsed,
    zIndex: card.z_index,
    dirtyContent: false,
    dirtyLayout: false,
    saving: false,
    error: undefined,
    updatedAt: card.updated_at,
    contentRevision: 0,
    layoutRevision: 0,
  };
}

/**
 * 取当前 threadId；为空时直接抛错，避免拼出 `/api/threads//whiteboard` 这种坏 URL。
 *
 * 由 store 内部各 action 调用：保证只有 `fetchBoard` / `clearBoard` 之外的动作
 * 都能拿到合法 threadId，否则提早暴露逻辑错误。
 */
function requireThreadId(threadId: string | null, action: string): string {
  if (!threadId) {
    throw new Error(
      `whiteboard store: 调用 ${action} 时未绑定 thread；先调 fetchBoard(threadId) 或在 thread 未选定时不要触发该动作`,
    );
  }
  return threadId;
}

export const useWhiteboardStore = create<WhiteboardState>((set, get) => ({
  currentThreadId: null,
  globalTitle: "Whiteboard",
  projectTitle: null,
  boardUpdatedAt: 0,
  cards: [],
  loading: false,
  selectedCardId: null,
  draggingCardId: null,
  resizingCardId: null,

  fetchBoard: async (threadId) => {
    set({ loading: true, currentThreadId: threadId });
    try {
      const board = await apiGet<WhiteboardDTO>(
        `/api/threads/${threadId}/whiteboard`,
      );
      // race guard：响应到达时校验 currentThreadId 仍是本次 fetch 的 threadId。
      // 用户快速切 thread A → B（或 A → 纯聊天 tab）时，A 的旧响应不应污染 B / null state。
      // 同时覆盖 R2 P0-1（旧响应覆盖新 thread）和 P0-2（clearBoard 后旧响应回填）两个场景。
      if (get().currentThreadId !== threadId) {
        return;
      }
      set({
        globalTitle: board.global_title,
        projectTitle: board.project_title,
        boardUpdatedAt: Date.now(),
        cards: mapBoard(board),
      });
    } finally {
      // loading 仍然清掉——即使响应被丢弃，loading 状态也得复位（否则会卡 loading）。
      // 只在当前 thread 仍匹配时复位 loading；否则保留当前 thread 自己的 loading 状态不动。
      if (get().currentThreadId === threadId) {
        set({ loading: false });
      }
    }
  },

  clearBoard: () => {
    // 切到纯聊天 tab（threadId=null）时清空所有本地白板状态
    contentTimers.forEach((timer) => window.clearTimeout(timer));
    contentTimers.clear();
    layoutSaveTimers.forEach((timer) => window.clearTimeout(timer));
    layoutSaveTimers.clear();
    set({
      currentThreadId: null,
      globalTitle: "Whiteboard",
      projectTitle: null,
      boardUpdatedAt: 0,
      cards: [],
      loading: false,
      selectedCardId: null,
      draggingCardId: null,
      resizingCardId: null,
    });
  },

  createCard: async ({
    scope,
    title,
    category,
    content = "",
    x = 24,
    y = 24,
    height = 260,
    collapsed,
  }) => {
    const threadId = requireThreadId(get().currentThreadId, "createCard");
    const body: CreateWhiteboardCardRequest = {
      scope,
      title,
      category,
      content,
      x,
      y,
      height,
      ...(collapsed !== undefined ? { collapsed } : {}),
    };
    const card = await apiPost<WhiteboardCardDTO>(
      `/api/threads/${threadId}/whiteboard/cards`,
      body,
    );
    set((state) => ({
      cards: [...state.cards, mapCard(card)].sort((a, b) => a.zIndex - b.zIndex),
      boardUpdatedAt: Date.now(),
    }));
  },

  updateCardContentLocal: (cardId, content) => {
    set((state) => ({
      cards: state.cards.map((card) =>
        card.id === cardId
          ? {
              ...card,
              content,
              dirtyContent: true,
              error: undefined,
              contentRevision: (card.contentRevision ?? 0) + 1,
            }
          : card,
      ),
    }));
    clearContentTimer(cardId);
    const timer = window.setTimeout(() => {
      void get().saveCardContent(cardId);
    }, CONTENT_SAVE_DELAY_MS);
    contentTimers.set(cardId, timer);
  },

  saveCardContent: async (cardId) => {
    clearContentTimer(cardId);
    const card = get().cards.find((item) => item.id === cardId);
    if (!card) return;
    if (!card.dirtyContent && !card.error) return;
    const threadId = requireThreadId(get().currentThreadId, "saveCardContent");
    const saveRevision = card.contentRevision ?? 0;

    set((state) => ({
      cards: state.cards.map((item) =>
        item.id === cardId
          ? {
              ...item,
              saving: true,
              error: undefined,
            }
          : item,
      ),
    }));

    const body: UpdateWhiteboardCardRequest = {
      title: card.title,
      category: card.category,
      content: card.content,
      expected_updated_at: card.updatedAt,
    };

    try {
      const updated = await apiPut<WhiteboardCardDTO>(
        `/api/threads/${threadId}/whiteboard/cards/${cardId}`,
        body,
      );
      set((state) => ({
        cards: state.cards.map((item) =>
          item.id !== cardId
            ? item
            : (item.contentRevision ?? 0) > saveRevision
              ? {
                  ...item,
                  saving: false,
                  error: undefined,
                  updatedAt: updated.updated_at,
                }
              : mapCard(updated),
        ),
        boardUpdatedAt: Date.now(),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      set((state) => ({
        cards: state.cards.map((item) =>
          item.id === cardId
            ? {
                ...item,
                saving: false,
                error: message,
              }
            : item,
        ),
      }));
      throw err;
    }
  },

  updateCardMetaLocal: (cardId, patch) => {
    set((state) => ({
      cards: state.cards.map((card) =>
        card.id === cardId
          ? {
              ...card,
              ...patch,
              dirtyContent: true,
              error: undefined,
              contentRevision: (card.contentRevision ?? 0) + 1,
            }
          : card,
      ),
    }));
    clearContentTimer(cardId);
    const timer = window.setTimeout(() => {
      void get().saveCardContent(cardId);
    }, CONTENT_SAVE_DELAY_MS);
    contentTimers.set(cardId, timer);
  },

  updateCardLayoutLocal: (cardId, patch) => {
    // 找出被改的卡所属 scope，用来安排本 scope 的 debounce 触发
    const targetScope = get().cards.find((card) => card.id === cardId)?.scope;
    set((state) => ({
      cards: state.cards.map((card) =>
        card.id === cardId
          ? {
              ...card,
              ...patch,
              dirtyLayout: true,
              error: undefined,
              layoutRevision: (card.layoutRevision ?? 0) + 1,
            }
          : card,
      ),
    }));
    if (targetScope === undefined) return;
    clearLayoutTimer(targetScope);
    const timer = window.setTimeout(() => {
      void get().saveLayout(targetScope);
    }, LAYOUT_SAVE_DELAY_MS);
    layoutSaveTimers.set(targetScope, timer);
  },

  saveLayout: async (scope) => {
    clearLayoutTimer(scope);
    const threadId = requireThreadId(get().currentThreadId, "saveLayout");
    const state = get();
    const dirtyCards = state.cards.filter(
      (card) => card.scope === scope && card.dirtyLayout,
    );
    if (dirtyCards.length === 0) return;
    // 当前 scope 全部卡片（包含 not dirty 的）一起作为 PATCH payload，
    // 让后端按 id 重新写整份 board.json layout 段
    const scopeCards = state.cards.filter((card) => card.scope === scope);
    const savedLayoutRevisions = new Map(
      scopeCards.map((card) => [card.id, card.layoutRevision ?? 0]),
    );

    const body: UpdateWhiteboardLayoutRequest = {
      scope,
      // 同一 scope 共享一个 title：global → globalTitle，project → projectTitle（null 时传 null）
      title:
        scope === "global" ? state.globalTitle : (state.projectTitle ?? null),
      cards: scopeCards.map((card) => ({
        id: card.id,
        x: card.x,
        y: card.y,
        height: card.height,
        collapsed: card.collapsed,
        z_index: card.zIndex,
      })),
    };

    try {
      const board = await apiPatch<WhiteboardDTO>(
        `/api/threads/${threadId}/whiteboard/layout`,
        body,
      );
      const boardCardsById = new Map(board.cards.map((card) => [card.id, card]));
      set({
        globalTitle: board.global_title,
        projectTitle: board.project_title,
        boardUpdatedAt: Date.now(),
        cards: get().cards.map((card) => {
          // 跨 scope 的卡不在本次 PATCH 范围里，原样保留
          if (card.scope !== scope) return card;
          const savedRevision = savedLayoutRevisions.get(card.id) ?? 0;
          if ((card.layoutRevision ?? 0) > savedRevision) {
            // 用户在 PATCH 飞行期间又改了这张卡 → 保留本地未保存的状态，只清 error
            return {
              ...card,
              error: undefined,
            };
          }
          const updated = boardCardsById.get(card.id);
          if (!updated) return card;
          return {
            ...mapCard(updated),
            contentRevision: card.contentRevision ?? 0,
            layoutRevision: savedRevision,
          };
        }),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      set((current) => ({
        cards: current.cards.map((card) =>
          card.scope === scope && card.dirtyLayout
            ? {
                ...card,
                error: message,
              }
            : card,
        ),
      }));
      throw err;
    }
  },

  deleteCard: async (cardId) => {
    const threadId = requireThreadId(get().currentThreadId, "deleteCard");
    await apiDelete(`/api/threads/${threadId}/whiteboard/cards/${cardId}`);
    set((state) => ({
      cards: state.cards.filter((card) => card.id !== cardId),
      selectedCardId:
        state.selectedCardId === cardId ? null : state.selectedCardId,
      draggingCardId:
        state.draggingCardId === cardId ? null : state.draggingCardId,
      resizingCardId:
        state.resizingCardId === cardId ? null : state.resizingCardId,
    }));
  },

  setSelectedCard: (cardId) => set({ selectedCardId: cardId }),
  setDraggingCard: (cardId) => set({ draggingCardId: cardId }),
  setResizingCard: (cardId) => set({ resizingCardId: cardId }),

  bringCardToFront: (cardId) => {
    const maxZ = get().cards.reduce(
      (currentMax, card) => Math.max(currentMax, card.zIndex),
      0,
    );
    get().updateCardLayoutLocal(cardId, { zIndex: maxZ + 1 });
  },

  toggleCollapsed: (cardId) => {
    const card = get().cards.find((item) => item.id === cardId);
    if (!card) return;
    get().updateCardLayoutLocal(cardId, { collapsed: !card.collapsed });
  },
}));
