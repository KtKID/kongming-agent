import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "@/lib/api";
import type {
  CreateWhiteboardCardRequest,
  WhiteboardCardDTO,
  UpdateWhiteboardCardRequest,
  UpdateWhiteboardLayoutRequest,
  WhiteboardDTO,
} from "@/protocol";

export interface WhiteboardCardState {
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
  boardTitle: string;
  boardUpdatedAt: number;
  cards: WhiteboardCardState[];
  loading: boolean;
  selectedCardId: string | null;
  draggingCardId: string | null;
  resizingCardId: string | null;
  fetchBoard: () => Promise<void>;
  createCard: (input: {
    title: string;
    category: string;
    content?: string;
    x?: number;
    y?: number;
    height?: number;
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
  saveLayout: () => Promise<void>;
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
let layoutSaveTimer: number | null = null;

function clearContentTimer(cardId: string): void {
  const timer = contentTimers.get(cardId);
  if (timer !== undefined) {
    window.clearTimeout(timer);
    contentTimers.delete(cardId);
  }
}

function mapBoard(dto: WhiteboardDTO): WhiteboardCardState[] {
  return dto.cards
    .slice()
    .sort((a, b) => a.z_index - b.z_index)
    .map((card) => ({
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
    }));
}

function mapCard(card: WhiteboardCardDTO): WhiteboardCardState {
  return {
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

export const useWhiteboardStore = create<WhiteboardState>((set, get) => ({
  boardTitle: "白板",
  boardUpdatedAt: 0,
  cards: [],
  loading: false,
  selectedCardId: null,
  draggingCardId: null,
  resizingCardId: null,

  fetchBoard: async () => {
    set({ loading: true });
    try {
      const board = await apiGet<WhiteboardDTO>("/api/whiteboard");
      set({
        boardTitle: board.title,
        boardUpdatedAt: Date.now(),
        cards: mapBoard(board),
      });
    } finally {
      set({ loading: false });
    }
  },

  createCard: async ({ title, category, content = "", x = 24, y = 24, height = 260 }) => {
    const body: CreateWhiteboardCardRequest = {
      title,
      category,
      content,
      x,
      y,
      height,
    };
    const card = await apiPost<WhiteboardCardDTO>("/api/whiteboard/cards", body);
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
        `/api/whiteboard/cards/${cardId}`,
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
    if (layoutSaveTimer !== null) {
      window.clearTimeout(layoutSaveTimer);
    }
    layoutSaveTimer = window.setTimeout(() => {
      void get().saveLayout();
    }, LAYOUT_SAVE_DELAY_MS);
  },

  saveLayout: async () => {
    if (layoutSaveTimer !== null) {
      window.clearTimeout(layoutSaveTimer);
      layoutSaveTimer = null;
    }
    const state = get();
    const dirtyCards = state.cards.filter((card) => card.dirtyLayout);
    if (dirtyCards.length === 0) return;
    const savedLayoutRevisions = new Map(
      state.cards.map((card) => [card.id, card.layoutRevision ?? 0]),
    );

    const body: UpdateWhiteboardLayoutRequest = {
      title: state.boardTitle,
      cards: state.cards.map((card) => ({
        id: card.id,
        x: card.x,
        y: card.y,
        height: card.height,
        collapsed: card.collapsed,
        z_index: card.zIndex,
      })),
    };

    try {
      const board = await apiPatch<WhiteboardDTO>("/api/whiteboard/layout", body);
      const boardCardsById = new Map(board.cards.map((card) => [card.id, card]));
      set({
        boardTitle: board.title,
        boardUpdatedAt: Date.now(),
        cards: get().cards.map((card) => {
          const savedRevision = savedLayoutRevisions.get(card.id) ?? 0;
          if ((card.layoutRevision ?? 0) > savedRevision) {
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
          card.dirtyLayout
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
    await apiDelete(`/api/whiteboard/cards/${cardId}`);
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
