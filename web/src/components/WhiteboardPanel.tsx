import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  FileText,
  Globe,
  LayoutGrid,
  ListTodo,
  Network,
  PanelRightClose,
  Workflow,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { WhiteboardCard, type WhiteboardCardItem } from "@/components/WhiteboardCard";
import { ArchitectureDiagramDialog } from "@/components/ArchitectureDiagramDialog";
import { WorkflowDashboard } from "@/components/WorkflowDashboard";
import { useWhiteboardResize } from "@/lib/whiteboard-resize";
import { cn } from "@/lib/utils";
import type { CardScope } from "@/protocol";
import type { WhiteboardCardKind } from "@/lib/whiteboard-card-templates";

export type { WhiteboardCardItem } from "@/components/WhiteboardCard";

interface WhiteboardPanelProps {
  title?: string;
  cards: WhiteboardCardItem[];
  isOpen?: boolean;
  compactMode?: boolean;
  mobileMode?: boolean;
  canCreate?: boolean;
  projectTitle?: string | null;
  emptyTitle?: string;
  emptyDescription?: string;
  onToggleOpen?: () => void;
  onCreateCard?: (scope: CardScope, kind: WhiteboardCardKind) => void;
  onToggleCollapse?: (cardId: string) => void;
  onDeleteCard?: (cardId: string) => void;
  onUpdateCard?: (cardId: string, patch: Partial<WhiteboardCardItem>) => void;
  onUpdateCardLayout?: (
    cardId: string,
    patch: Partial<Pick<WhiteboardCardItem, "x" | "y" | "height" | "zIndex">>,
  ) => void;
  onBringToFront?: (cardId: string) => void;
}

const MIN_CARD_HEIGHT = 160;
const DEFAULT_CARD_HEIGHT = 200;
const COLLAPSED_CARD_HEIGHT = 68;

export function WhiteboardPanel({
  title = "Whiteboard",
  cards,
  isOpen = true,
  compactMode = false,
  mobileMode = false,
  canCreate = true,
  projectTitle,
  emptyTitle = "还没有卡片",
  emptyDescription = "新建便签记录想法，或新建待办开始拆动作。",
  onToggleOpen,
  onCreateCard,
  onToggleCollapse,
  onDeleteCard,
  onUpdateCard,
  onUpdateCardLayout,
  onBringToFront,
}: WhiteboardPanelProps) {
  const hasCreateHandler = canCreate && Boolean(onCreateCard);
  const primaryCreateScope: CardScope = projectTitle === null ? "global" : "project";
  const projectButtonDisabled = !hasCreateHandler;
  const projectButtonTooltip =
    projectTitle === null ? "当前会话未绑定项目，将创建全局卡片" : "新建项目卡片";
  const globalButtonDisabled = !hasCreateHandler;
  const [diagramDialogOpen, setDiagramDialogOpen] = useState(false);
  const [workflowDashboardOpen, setWorkflowDashboardOpen] = useState(false);

  const resizeEnabled = isOpen && !compactMode && !mobileMode;
  const panelResize = useWhiteboardResize({ enabled: resizeEnabled });
  const boardRef = useRef<HTMLDivElement | null>(null);
  const interactionRef = useRef<{
    type: "drag" | "resize";
    cardId: string;
    pointerId: number;
    originX: number;
    originY: number;
    startX: number;
    startY: number;
    startHeight: number;
  } | null>(null);

  const cardsWithDefaults = useMemo(
    () =>
      cards.map((card, index) => ({
        ...card,
        x: card.x ?? 20 + index * 18,
        y: card.y ?? 20 + index * 18,
        zIndex: card.zIndex ?? index + 1,
      })),
    [cards],
  );

  const canvasHeight = useMemo(() => {
    if (cardsWithDefaults.length === 0) return 520;
    return Math.max(
      520,
      ...cardsWithDefaults.map((card) =>
        (card.y ?? 0) +
        (card.collapsed
          ? COLLAPSED_CARD_HEIGHT
          : Math.max(card.height ?? DEFAULT_CARD_HEIGHT, MIN_CARD_HEIGHT)) +
        12,
      ),
    );
  }, [cardsWithDefaults]);

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      const interaction = interactionRef.current;
      const board = boardRef.current;
      if (!interaction || !board) return;
      const targetCard = cardsWithDefaults.find((card) => card.id === interaction.cardId);
      if (!targetCard) return;

      const rect = board.getBoundingClientRect();
      if (interaction.type === "drag") {
        const deltaX = event.clientX - interaction.originX;
        const deltaY = event.clientY - interaction.originY;
        const nextX = Math.max(0, Math.min(interaction.startX + deltaX, rect.width - 300));
        const nextY = Math.max(0, Math.min(interaction.startY + deltaY, canvasHeight - COLLAPSED_CARD_HEIGHT));
        onUpdateCardLayout?.(interaction.cardId, { x: Math.round(nextX), y: Math.round(nextY) });
        return;
      }

      const deltaY = event.clientY - interaction.originY;
      const nextHeight = Math.max(
        120,
        Math.min(interaction.startHeight + deltaY, canvasHeight - (targetCard.y ?? 0) - 16),
      );
      onUpdateCardLayout?.(interaction.cardId, { height: Math.round(nextHeight) });
    };

    const onPointerUp = (event: PointerEvent) => {
      const interaction = interactionRef.current;
      if (!interaction || interaction.pointerId !== event.pointerId) return;
      interactionRef.current = null;
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
  }, [canvasHeight, cardsWithDefaults, onUpdateCardLayout]);

  const startDrag = (cardId: string, event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const targetCard = cardsWithDefaults.find((card) => card.id === cardId);
    if (!targetCard) return;
    interactionRef.current = {
      type: "drag",
      cardId,
      pointerId: event.pointerId,
      originX: event.clientX,
      originY: event.clientY,
      startX: targetCard.x ?? 0,
      startY: targetCard.y ?? 0,
      startHeight: targetCard.height ?? DEFAULT_CARD_HEIGHT,
    };
    onBringToFront?.(cardId);
  };

  const startResize = (cardId: string, event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const targetCard = cardsWithDefaults.find((card) => card.id === cardId);
    if (!targetCard) return;
    interactionRef.current = {
      type: "resize",
      cardId,
      pointerId: event.pointerId,
      originX: event.clientX,
      originY: event.clientY,
      startX: targetCard.x ?? 0,
      startY: targetCard.y ?? 0,
      startHeight: targetCard.height ?? DEFAULT_CARD_HEIGHT,
    };
    onBringToFront?.(cardId);
  };

  return (
    <>
      {mobileMode && isOpen ? (
        <button
          type="button"
          aria-label="关闭白板遮罩"
          onClick={onToggleOpen}
          className="absolute inset-0 z-10 bg-background/45 backdrop-blur-[1px]"
        />
      ) : null}
      <aside
        data-testid="whiteboard-panel"
        className={cn(
          "obsidian-panel obsidian-hairline relative z-20 flex h-full shrink-0 flex-col rounded-[1.85rem]",
          panelResize.isResizing
            ? "transition-[min-width,transform] duration-300 ease-out"
            : "transition-[width,min-width,transform] duration-300 ease-out",
          compactMode ? "absolute inset-y-0 right-0 shadow-xl" : "shadow-none",
          isOpen
            ? mobileMode
              ? "w-[90vw] min-w-0 overflow-hidden"
              : compactMode
                ? "w-[min(50rem,calc(100vw-5.5rem))] min-w-0"
                : "min-w-[24rem]"
            : mobileMode
              ? "w-0 min-w-0 overflow-visible border-l-0"
              : "w-[4.5rem] min-w-[4.5rem] overflow-hidden",
        )}
        style={resizeEnabled ? { width: `${panelResize.width}px` } : undefined}
      >
        {isOpen && !mobileMode ? (
          <button
            type="button"
            onClick={onToggleOpen}
            className="absolute left-0 top-6 z-20 inline-flex h-11 w-11 -translate-x-1/2 items-center justify-center rounded-[1.3rem] border border-border/80 bg-card/90 text-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-card"
            aria-label="隐藏白板"
          >
            <PanelRightClose className="h-4.5 w-4.5" />
          </button>
        ) : null}
        {resizeEnabled ? (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="拖拽调整白板宽度（双击复位）"
            data-testid="whiteboard-resize-handle"
            data-resizing={panelResize.isResizing ? "true" : "false"}
            onPointerDown={panelResize.startResize}
            onDoubleClick={panelResize.resetWidth}
            className={cn(
              "absolute left-0 top-20 bottom-0 z-30 w-1 cursor-col-resize touch-none transition-colors duration-150",
              panelResize.isResizing ? "bg-primary/60" : "bg-transparent hover:bg-primary/30",
            )}
          />
        ) : null}
        <div
          className={cn(
            "flex h-full flex-col origin-right transition-[opacity,transform] duration-300 ease-out",
            isOpen
              ? "translate-x-0 scale-x-100 opacity-100"
              : "pointer-events-none translate-x-8 scale-x-95 opacity-0",
          )}
        >
          <div className="border-b border-border/70 px-4 py-3.5 pl-8">
            <div className="flex items-start justify-between gap-3">
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold tracking-tight text-foreground text-glow">
                    {title}
                  </span>
                  <span className="rounded-full border border-border/80 bg-background/50 px-2 py-0.5 text-[10px] text-muted-foreground">
                    {cards.length} cards
                  </span>
                </div>
                <p className="text-[11px] leading-5 text-muted-foreground">
                  直接记想法，直接拆待办。
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-1.5">
                <Button
                  type="button"
                  size="sm"
                  onClick={() => onCreateCard?.(primaryCreateScope, "note")}
                  disabled={projectButtonDisabled}
                  title={projectTitle === null ? projectButtonTooltip : "新建便签"}
                  aria-label="新建便签"
                  className={cn(
                    "gap-1.5 border-border/80 bg-card/85 text-foreground hover:bg-card",
                    projectButtonDisabled && "cursor-not-allowed",
                  )}
                >
                  <FileText className="h-3.5 w-3.5" />
                  便签
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => onCreateCard?.(primaryCreateScope, "todo")}
                  disabled={projectButtonDisabled}
                  title={projectTitle === null ? projectButtonTooltip : "新建待办"}
                  aria-label="新建待办"
                  className={cn(
                    "gap-1.5 border-border/80 bg-background/75 text-foreground hover:bg-card",
                    projectButtonDisabled && "cursor-not-allowed",
                  )}
                >
                  <ListTodo className="h-3.5 w-3.5" />
                  待办
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => onCreateCard?.("global", "note")}
                  disabled={globalButtonDisabled}
                  title="新建全局便签"
                  aria-label="新建全局便签"
                  className={cn(
                    "h-8 w-8 px-0 opacity-70 transition-opacity hover:opacity-100",
                    globalButtonDisabled && "cursor-not-allowed",
                  )}
                >
                  <Globe className="h-4 w-4" />
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setWorkflowDashboardOpen(true)}
                  className="h-8 w-8 px-0 opacity-70 transition-opacity hover:opacity-100"
                  title="工作流"
                  aria-label="工作流"
                >
                  <Workflow className="h-4 w-4" />
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setDiagramDialogOpen(true)}
                  className="h-8 w-8 px-0 opacity-70 transition-opacity hover:opacity-100"
                  title="架构图"
                  aria-label="架构图"
                >
                  <Network className="h-4 w-4" />
                </Button>
              </div>
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-hidden pb-1.5">
            <div
              ref={boardRef}
              className="relative h-full overflow-hidden rounded-[1.4rem] border border-border/70 bg-card/95 shadow-glass"
            >
              <div className="obsidian-grid pointer-events-none absolute inset-0 opacity-[0.18]" />
              <div className="relative h-full overflow-y-auto scrollbar-overlay">
                {cards.length === 0 ? (
                  <div className="flex h-full items-center justify-center p-4">
                    <div className="w-full max-w-sm rounded-[1.35rem] border border-dashed border-border/80 bg-card/72 px-5 py-7 text-center shadow-glass backdrop-blur-sm">
                      <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-2xl border border-border/80 bg-muted/70 text-muted-foreground">
                        <LayoutGrid className="h-4.5 w-4.5" />
                      </div>
                      <div className="text-sm font-semibold tracking-tight text-foreground">
                        {emptyTitle}
                      </div>
                      <p className="mt-2 text-xs leading-5 text-muted-foreground">
                        {emptyDescription}
                      </p>
                      <div className="mt-4 flex items-center justify-center gap-2">
                        <Button
                          type="button"
                          size="sm"
                          onClick={() => onCreateCard?.(primaryCreateScope, "note")}
                          disabled={projectButtonDisabled}
                          title={projectTitle === null ? projectButtonTooltip : "新建便签"}
                          className={cn(
                            "gap-1.5 border-border/80 bg-card/85 text-foreground hover:bg-card",
                            projectButtonDisabled && "cursor-not-allowed",
                          )}
                        >
                          <FileText className="h-3.5 w-3.5" />
                          便签
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => onCreateCard?.(primaryCreateScope, "todo")}
                          disabled={projectButtonDisabled}
                          title={projectTitle === null ? projectButtonTooltip : "新建待办"}
                          className={cn(
                            "gap-1.5 border-border/80 bg-background/75 text-foreground hover:bg-card",
                            projectButtonDisabled && "cursor-not-allowed",
                          )}
                        >
                          <ListTodo className="h-3.5 w-3.5" />
                          待办
                        </Button>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="relative min-h-full px-1.5 py-2" style={{ minHeight: canvasHeight }}>
                    {cardsWithDefaults.map((card) => (
                      <div
                        key={card.id}
                        className="absolute w-[18.5rem] max-w-[calc(100%-1rem)]"
                        style={{
                          left: card.x,
                          top: card.y,
                          zIndex: card.zIndex,
                        }}
                      >
                        <WhiteboardCard
                          card={card}
                          onDragPointerDown={(event) => startDrag(card.id, event)}
                          onResizePointerDown={(event) => startResize(card.id, event)}
                          onToggleCollapse={onToggleCollapse}
                          onDeleteCard={onDeleteCard}
                          onUpdateCard={onUpdateCard}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
        <div
          className={cn(
            "absolute inset-0 transition-[opacity,transform] duration-300 ease-out",
            isOpen ? "pointer-events-none translate-x-6 opacity-0" : "translate-x-0 opacity-100",
          )}
        >
          <button
            type="button"
            onClick={onToggleOpen}
            aria-label="展开白板"
            data-testid="whiteboard-edge-handle"
            className={cn(
              "inline-flex items-center justify-center border border-border/80 bg-card/90 text-foreground shadow-glass backdrop-blur-xl transition-colors hover:bg-card",
              mobileMode
                ? "absolute right-0 top-5 h-20 w-6 rounded-l-2xl border-r-0"
                : "mt-5 mr-2 h-12 w-12 rounded-[1.4rem]",
            )}
          >
            <ChevronLeft className={cn(mobileMode ? "h-4 w-4" : "h-4.5 w-4.5")} />
          </button>
        </div>
      </aside>
      <ArchitectureDiagramDialog
        open={diagramDialogOpen}
        onOpenChange={setDiagramDialogOpen}
      />
      <WorkflowDashboard
        open={workflowDashboardOpen}
        onOpenChange={setWorkflowDashboardOpen}
      />
    </>
  );
}
