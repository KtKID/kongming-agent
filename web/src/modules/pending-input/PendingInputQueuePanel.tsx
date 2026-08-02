import { useEffect, useRef, useState, type PointerEvent } from "react";
import { Check, Edit3, GripVertical, Send, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type { PendingInputDTO } from "@/protocol";

interface PendingInputQueuePanelProps {
  /** 服务端确认尚未启动的队列项；展示顺序直接使用 items 顺序。 */
  items: PendingInputDTO[];
  /** 服务端队列上限，用于容量提示。 */
  maxItems: number;
  /** 队列操作失败的可见错误文案。 */
  error?: string | null;
  /** 禁用编辑、删除和排序按钮，但保留队列可见性。 */
  disabled?: boolean;
  /** 提交编辑后的内容；调用方负责发 pending-input.update 帧。 */
  onUpdate: (id: string, content: string) => void;
  /** 删除尚未启动的队列项；调用方负责发 pending-input.cancel 帧。 */
  onCancel: (id: string) => void;
  /** 请求后端立即发送该队列项；调用方负责发 pending-input.send-now 帧。 */
  onSendNow: (id: string) => void;
  /** 提交拖拽松手后的完整队列顺序；调用方负责发 pending-input.reorder 帧。 */
  onReorder: (orderedIds: string[]) => void;
}

const SOURCE_LABEL: Record<PendingInputDTO["source"], string> = {
  user_input: "消息",
  choice_submit: "选择",
  avatar: "Avatar",
};

/** 队列展示组件：只渲染和发起用户意图，不持有服务端真源状态。 */
export function PendingInputQueuePanel({
  items,
  maxItems,
  error = null,
  disabled = false,
  onUpdate,
  onCancel,
  onSendNow,
  onReorder,
}: PendingInputQueuePanelProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [orderedItems, setOrderedItems] = useState<PendingInputDTO[]>(items);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const itemRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const orderedItemsRef = useRef<PendingInputDTO[]>(items);
  const draggingIdRef = useRef<string | null>(null);
  const itemsRef = useRef<PendingInputDTO[]>(items);

  useEffect(() => {
    itemsRef.current = items;
    if (draggingId) {
      const serverById = new Map(items.map((item) => [item.id, item]));
      const nextItems = orderedItemsRef.current
        .filter((item) => serverById.has(item.id))
        .map((item) => serverById.get(item.id) ?? item);
      const nextIds = new Set(nextItems.map((item) => item.id));
      for (const item of items) {
        if (!nextIds.has(item.id)) {
          nextItems.push(item);
        }
      }
      orderedItemsRef.current = nextItems;
      setOrderedItems(nextItems);
      if (!serverById.has(draggingId)) {
        draggingIdRef.current = null;
        setDraggingId(null);
      }
      return;
    }
    setOrderedItems(items);
    orderedItemsRef.current = items;
  }, [draggingId, items]);

  useEffect(() => {
    orderedItemsRef.current = orderedItems;
  }, [orderedItems]);

  /** 把服务端队列项复制到本地编辑草稿；保存前只影响组件内部状态。 */
  const startEdit = (item: PendingInputDTO) => {
    setEditingId(item.id);
    setDraft(item.content);
  };

  /** 提交编辑草稿；最终内容和顺序等待服务端 changed 帧回写。 */
  const commitEdit = () => {
    if (!editingId) return;
    const next = draft.trim();
    if (!next) return;
    onUpdate(editingId, next);
    setEditingId(null);
    setDraft("");
  };

  /** 丢弃本地编辑草稿，保留服务端队列快照。 */
  const cancelEdit = () => {
    setEditingId(null);
    setDraft("");
  };

  /** 根据指针 Y 坐标把正在拖动的队列项插入到目标行前后。 */
  const reorderByPointer = (
    currentItems: PendingInputDTO[],
    activeId: string,
    clientY: number,
  ): PendingInputDTO[] => {
    const active = currentItems.find((entry) => entry.id === activeId);
    if (!active) return currentItems;

    const remaining = currentItems.filter((entry) => entry.id !== activeId);
    let targetIndex = remaining.length;
    for (let index = 0; index < remaining.length; index += 1) {
      const rect = itemRefs.current[remaining[index].id]?.getBoundingClientRect();
      if (!rect) continue;
      if (clientY < rect.top + rect.height / 2) {
        targetIndex = index;
        break;
      }
    }

    return [
      ...remaining.slice(0, targetIndex),
      active,
      ...remaining.slice(targetIndex),
    ];
  };

  /** 开始拖拽时锁定当前服务端快照，后续移动只更新本地视觉顺序。 */
  const beginDrag = (item: PendingInputDTO, event: PointerEvent<HTMLButtonElement>) => {
    if (disabled || editingId === item.id) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    orderedItemsRef.current = items;
    setOrderedItems(items);
    draggingIdRef.current = item.id;
    setDraggingId(item.id);
  };

  /** 拖拽过程中只调整本地展示顺序，避免频繁写后端。 */
  const moveDrag = (event: PointerEvent<HTMLButtonElement>) => {
    const activeId = draggingIdRef.current;
    if (!activeId) return;
    event.preventDefault();
    const nextItems = reorderByPointer(orderedItemsRef.current, activeId, event.clientY);
    const currentIds = orderedItemsRef.current.map((entry) => entry.id).join("\n");
    const nextIds = nextItems.map((entry) => entry.id).join("\n");
    if (currentIds === nextIds) return;
    orderedItemsRef.current = nextItems;
    setOrderedItems(nextItems);
  };

  /** 松手后提交完整 ID 顺序，最终队列状态等待服务端 changed 帧回写。 */
  const finishDrag = (event: PointerEvent<HTMLButtonElement>) => {
    const activeId = draggingIdRef.current;
    if (!activeId) return;
    event.preventDefault();
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }

    const serverIds = itemsRef.current.map((entry) => entry.id);
    const serverIdSet = new Set(serverIds);
    const nextIds = orderedItemsRef.current
      .map((entry) => entry.id)
      .filter((id) => serverIdSet.has(id));
    const nextIdSet = new Set(nextIds);
    for (const id of serverIds) {
      if (!nextIdSet.has(id)) {
        nextIds.push(id);
      }
    }
    const changed =
      serverIdSet.has(activeId) &&
      nextIds.length === serverIds.length &&
      nextIds.some((id, index) => id !== serverIds[index]);
    draggingIdRef.current = null;
    setDraggingId(null);
    if (changed) {
      onReorder(nextIds);
    }
  };

  if (items.length === 0) return null;

  const displayItems = draggingId ? orderedItems : items;

  return (
    <div
      className="border-t border-border/60 bg-background/35 px-2 py-2"
      data-testid="pending-input-queue"
    >
      <div className="mb-1.5 flex items-center justify-between gap-2 px-1 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">待发送</span>
        <span>
          {items.length}/{maxItems}
        </span>
      </div>
      {error ? (
        <div
          className="mb-1.5 rounded-md border border-destructive/25 bg-destructive/10 px-2 py-1 text-xs text-destructive"
          data-testid="pending-input-error"
        >
          {error}
        </div>
      ) : null}
      <div className="flex max-h-44 flex-col gap-1.5 overflow-y-auto pr-1">
        {displayItems.map((item) => {
          const editing = editingId === item.id;
          return (
            <div
              key={item.id}
              ref={(element) => {
                itemRefs.current[item.id] = element;
              }}
              className={cn(
                "grid grid-cols-[2.75rem_auto_minmax(0,1fr)_auto] items-stretch gap-2 overflow-hidden rounded-md border border-border/70 bg-card/72",
                item.priority === "choice_response" && "border-primary/45 bg-primary/5",
                draggingId === item.id && "border-primary/60 bg-primary/10 shadow-sm",
              )}
              data-testid="pending-input-item"
            >
              <button
                type="button"
                className={cn(
                  "flex min-h-14 w-11 touch-none select-none items-center justify-center self-stretch border-r border-border/45 bg-muted/45 text-muted-foreground",
                  disabled || editing
                    ? "cursor-not-allowed opacity-45"
                    : "cursor-grab active:cursor-grabbing hover:bg-muted/70 hover:text-foreground",
                )}
                onPointerDown={(event) => beginDrag(item, event)}
                onPointerMove={moveDrag}
                onPointerUp={finishDrag}
                onPointerCancel={finishDrag}
                disabled={disabled || editing}
                aria-label="拖动待发送消息"
                title="拖动排序"
                data-testid="pending-input-drag-handle"
              >
                <GripVertical className="h-5 w-5" />
              </button>
              <div className="flex items-start py-2">
                <div className="flex h-7 min-w-10 items-center justify-center rounded bg-muted px-2 text-[11px] font-medium text-muted-foreground">
                  {SOURCE_LABEL[item.source]}
                </div>
              </div>
              <div className="min-w-0 py-2">
                {editing ? (
                  <Textarea
                    value={draft}
                    rows={2}
                    onChange={(event) => setDraft(event.target.value)}
                    className="min-h-[3.25rem] resize-none text-sm leading-5"
                    data-testid="pending-input-edit"
                    aria-label="编辑待发送消息"
                    disabled={disabled}
                  />
                ) : (
                  <p className="whitespace-pre-wrap break-words text-sm leading-5 text-foreground">
                    {item.content}
                  </p>
                )}
              </div>
              <div className="flex flex-wrap justify-end gap-1 py-2 pr-2">
                {editing ? (
                  <>
                    <Button
                      type="button"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={commitEdit}
                      disabled={disabled || draft.trim().length === 0}
                      title="保存"
                      aria-label="保存待发送消息"
                    >
                      <Check className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={cancelEdit}
                      title="取消"
                      aria-label="取消编辑"
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      type="button"
                      variant="ghost"
                      className="h-7 w-7"
                      onClick={() => startEdit(item)}
                      disabled={disabled}
                      title="编辑"
                      aria-label="编辑待发送消息"
                    >
                      <Edit3 className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      className="h-7 w-7 text-destructive hover:text-destructive"
                      onClick={() => onCancel(item.id)}
                      disabled={disabled}
                      title="删除"
                      aria-label="删除待发送消息"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="h-7 gap-1 px-2 text-xs"
                      onClick={() => onSendNow(item.id)}
                      disabled={disabled}
                      title="立即发送"
                      aria-label="立即发送待发送消息"
                    >
                      <Send className="h-3.5 w-3.5" />
                      <span>立即发送</span>
                    </Button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
