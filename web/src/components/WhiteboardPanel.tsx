import { useEffect, useMemo, useRef } from "react";
import { ChevronLeft, LayoutGrid, PanelRightClose, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { WhiteboardCard, type WhiteboardCardItem } from "@/components/WhiteboardCard";
import { cn } from "@/lib/utils";

export type { WhiteboardCardItem } from "@/components/WhiteboardCard";

interface WhiteboardPanelProps {
  title?: string;
  cards: WhiteboardCardItem[];
  isOpen?: boolean;
  compactMode?: boolean;
  mobileMode?: boolean;
  canCreate?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  onToggleOpen?: () => void;
  onCreateCard?: () => void;
  onToggleCollapse?: (cardId: string) => void;
  onDeleteCard?: (cardId: string) => void;
  onUpdateCard?: (cardId: string, patch: Partial<WhiteboardCardItem>) => void;
  onUpdateCardLayout?: (
    cardId: string,
    patch: Partial<Pick<WhiteboardCardItem, "x" | "y" | "height" | "zIndex">>,
  ) => void;
  onBringToFront?: (cardId: string) => void;
}

export function WhiteboardPanel({
  title = "白板",
  cards,
  isOpen = true,
  compactMode = false,
  mobileMode = false,
  canCreate = true,
  emptyTitle = "还没有白板卡片",
  emptyDescription = "从右上角新建一张 markdown 卡片，文件会落到当前 workspace 的 whiteboard 目录。",
  onToggleOpen,
  onCreateCard,
  onToggleCollapse,
  onDeleteCard,
  onUpdateCard,
  onUpdateCardLayout,
  onBringToFront,
}: WhiteboardPanelProps) {
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
        x: card.x ?? 24 + index * 24,
        y: card.y ?? 24 + index * 24,
        zIndex: card.zIndex ?? index + 1,
      })),
    [cards],
  );
  const canvasHeight = useMemo(() => {
    if (cardsWithDefaults.length === 0) return 720;
    return Math.max(
      720,
      ...cardsWithDefaults.map((card) => (card.y ?? 0) + (card.collapsed ? 72 : Math.max(card.height ?? 280, 220)) + 24),
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
        const nextX = Math.max(0, Math.min(interaction.startX + deltaX, rect.width - 320));
        const nextY = Math.max(0, Math.min(interaction.startY + deltaY, canvasHeight - 72));
        onUpdateCardLayout?.(interaction.cardId, { x: Math.round(nextX), y: Math.round(nextY) });
        return;
      }

      const deltaY = event.clientY - interaction.originY;
      const nextHeight = Math.max(120, Math.min(interaction.startHeight + deltaY, canvasHeight - targetCard.y! - 16));
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
      startHeight: targetCard.height ?? 280,
    };
    onBringToFront?.(cardId);
  };

  const startResize = (cardId: string, event: React.PointerEvent<HTMLButtonElement>) => {
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
      startHeight: targetCard.height ?? 280,
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
      className={cn(
        "relative z-20 flex h-full shrink-0 flex-col border-l border-border/80",
        "bg-[linear-gradient(180deg,hsl(var(--background)/0.98),hsl(var(--muted)/0.7))] dark:bg-[linear-gradient(180deg,hsl(var(--background)/0.98),hsl(var(--card)/0.98))]",
        "transition-[width,min-width,transform] duration-300 ease-out",
        compactMode ? "absolute inset-y-0 right-0 shadow-xl" : "shadow-none",
        isOpen
          ? mobileMode
            ? "w-[min(24rem,calc(100vw-2rem))] min-w-0 overflow-hidden"
            : compactMode
            ? "w-[min(24rem,calc(100vw-5.5rem))] min-w-0"
            : "w-[26rem] min-w-[22rem]"
          : mobileMode
            ? "w-0 min-w-0 overflow-visible border-l-0"
            : "w-[4.5rem] min-w-[4.5rem] overflow-hidden",
      )}
    >
      {isOpen && !mobileMode ? (
        <button
          type="button"
          onClick={onToggleOpen}
          className="absolute left-0 top-5 z-20 inline-flex h-11 w-11 -translate-x-1/2 items-center justify-center rounded-[1.3rem] border border-border/80 bg-background/92 text-foreground shadow-sm backdrop-blur-sm transition-colors hover:bg-card"
          aria-label="隐藏白板"
        >
          <PanelRightClose className="h-4.5 w-4.5" />
        </button>
      ) : null}
      <div
        className={cn(
          "flex h-full flex-col origin-right transition-[opacity,transform] duration-300 ease-out",
          isOpen
            ? "translate-x-0 scale-x-100 opacity-100"
            : "pointer-events-none translate-x-8 scale-x-95 opacity-0",
        )}
      >
        <div className="border-b border-border/80 px-4 py-4 pl-7">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-1">
              <div className="text-[11px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
                workspace whiteboard
              </div>
              <div className="flex items-center gap-2">
                <span className="text-base font-semibold tracking-tight text-foreground">
                  {title}
                </span>
                <span className="rounded-full border border-border/80 bg-card/80 px-2 py-0.5 text-[11px] text-muted-foreground">
                  markdown cards
                </span>
              </div>
              <p className="max-w-xs text-xs leading-5 text-muted-foreground">
                白板是右侧卡片区域的名称。当前版本聚焦 markdown 便签卡片，不扩展为绘图白板。
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                type="button"
                size="sm"
                onClick={onCreateCard}
                disabled={!canCreate || !onCreateCard}
                className="border-border/80 bg-card/85 text-foreground hover:bg-card"
              >
                <Plus className="h-3.5 w-3.5" />
                新建
              </Button>
            </div>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden p-3">
          <div
            ref={boardRef}
            className={cn(
              "relative h-full overflow-hidden rounded-[1.5rem] border border-border/80",
              "bg-[linear-gradient(180deg,hsl(var(--card)/0.96),hsl(var(--muted)/0.72))] shadow-sm dark:bg-[linear-gradient(180deg,hsl(var(--card)/0.98),hsl(var(--background)/0.96))]",
            )}
          >
            <div className="pointer-events-none absolute left-4 top-4 z-10 inline-flex rounded-full border border-border/80 bg-background/85 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.2em] text-muted-foreground shadow-sm backdrop-blur-sm">
              白板区域
            </div>
            <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,hsl(var(--border)/0.38)_1px,transparent_1px),linear-gradient(to_bottom,hsl(var(--border)/0.38)_1px,transparent_1px)] bg-[size:18px_18px] opacity-[0.35] dark:opacity-[0.2]" />
            <div className="pointer-events-none absolute right-5 top-5 h-6 w-6 rounded-full border border-border/80 bg-accent/20 shadow-sm" />
            <div className="relative h-full overflow-y-auto scrollbar-overlay">
              {cards.length === 0 ? (
                <div className="flex h-full items-center justify-center p-5">
                  <div className="w-full max-w-sm rounded-[1.5rem] border border-dashed border-border/80 bg-card/72 px-5 py-8 text-center shadow-sm backdrop-blur-sm">
                    <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-2xl border border-border/80 bg-muted/70 text-muted-foreground">
                      <LayoutGrid className="h-5 w-5" />
                    </div>
                    <div className="text-sm font-semibold tracking-tight text-foreground">
                      {emptyTitle}
                    </div>
                    <p className="mt-2 text-xs leading-5 text-muted-foreground">
                      {emptyDescription}
                    </p>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={onCreateCard}
                      disabled={!canCreate || !onCreateCard}
                      className="mt-4 border-border/80 bg-card/85 text-foreground hover:bg-card"
                    >
                      <Plus className="h-3.5 w-3.5" />
                      新建第一张卡片
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="relative min-h-full p-4" style={{ minHeight: canvasHeight }}>
                  {cardsWithDefaults.map((card) => (
                    <div
                      key={card.id}
                      className="absolute w-[20rem] max-w-[calc(100%-1rem)]"
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
          isOpen
            ? "pointer-events-none translate-x-6 opacity-0"
            : "translate-x-0 opacity-100",
        )}
      >
        <button
          type="button"
          onClick={onToggleOpen}
          aria-label="展开白板"
          data-testid="whiteboard-edge-handle"
          className={cn(
            "inline-flex items-center justify-center border border-border/80 bg-card/88 text-foreground shadow-sm backdrop-blur-sm transition-colors hover:bg-card",
            mobileMode
              ? "absolute right-0 top-5 h-20 w-6 rounded-l-2xl border-r-0"
              : "mt-5 mr-2 h-12 w-12 rounded-[1.4rem]",
          )}
        >
          <ChevronLeft className={cn(mobileMode ? "h-4 w-4" : "h-4.5 w-4.5")} />
        </button>
      </div>
    </aside>
    </>
  );
}
