import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Pencil, Pin, PinOff, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useThreadsStore } from "@/stores/threads";
import { useThreadStatusStore } from "@/stores/threadStatus";
import { SidebarSessionRow, type HoverAction } from "@/components/SidebarSessionRow";
import { useInlineEdit } from "@/hooks/useInlineEdit";
import { PhaseIndicator } from "@/components/PhaseIndicator";
import { useState } from "react";
import { ThreadSourceIcon } from "@/components/ThreadSourceIcon";

/**
 * 左侧 thread 列表：
 * - 只显示 `backend_kind === "generic_chat"` 的 thread
 * - 旧 legacy claude（backend_kind=generic_chat 但 claude_thread_id 非空）
 *   仍会出现在这里，带 Claude 来源图标，避免老数据被藏起来
 * - updated_at 倒序（store 自动排序）
 * - 选中高亮（参数 thread_id）
 * - SidebarSessionRow 复用行原语
 * - "新建对话" 按钮 → 通用频道 pending 空白页
 */
export function ThreadList() {
  const params = useParams<{ thread_id?: string }>();
  const navigate = useNavigate();
  const threads = useThreadsStore((s) => s.threads);
  const fetchPresets = useThreadsStore((s) => s.fetchPresets);
  const renameThread = useThreadsStore((s) => s.renameThread);
  const deleteThread = useThreadsStore((s) => s.deleteThread);
  const pinThread = useThreadsStore((s) => s.pinThread);
  const startPendingGenericThread = useThreadsStore((s) => s.startPendingGenericThread);
  const statuses = useThreadStatusStore((s) => s.statuses);

  // 内联编辑（当前正在编辑的 thread id）
  const [editingId, setEditingId] = useState<string | null>(null);
  const { editing, startEdit, inputProps } = useInlineEdit({
    onConfirm: async (newName) => {
      if (!editingId) return;
      try {
        await renameThread(editingId, newName);
        toast.success("已重命名");
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(`重命名失败：${msg}`);
      } finally {
        setEditingId(null);
      }
    },
    onCancel: () => setEditingId(null),
  });

  useEffect(() => {
    void fetchPresets();
  }, [fetchPresets]);

  // 通用 tab 只显示 generic_chat thread。Claude / Codex 走各自 tab；
  // 旧 legacy claude（backend_kind=generic_chat 但 claude_thread_id 非空）
  // 会保留在这里，由 ThreadSourceIcon 渲染 Claude 图标。
  const visibleThreads = threads.filter(
    (t) => t.backend_kind === "generic_chat",
  );

  const onDelete = async (id: string) => {
    if (!window.confirm("确定删除这个 thread？历史不可恢复。")) return;
    try {
      await deleteThread(id);
      toast.success("已删除");
      if (params.thread_id === id) navigate("/chat", { replace: true });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`删除失败：${msg}`);
    }
  };

  return (
    <aside className="flex h-full w-full flex-col">
      <div className="border-b border-border/70 p-2">
        <Button
          size="sm"
          variant="secondary"
          className="w-full justify-center"
          onClick={() => {
            startPendingGenericThread();
            navigate("/chat");
          }}
          aria-label="新建对话"
        >
          <Plus className="h-4 w-4" />
          新建对话
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="flex flex-col gap-2 p-3">
          {visibleThreads.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-border/80 bg-card/40 px-3 py-6 text-center text-xs text-muted-foreground">
              还没有会话
            </div>
          ) : null}
          {visibleThreads.map((t) => {
            const active = params.thread_id === t.id;
            const isEditing = editing && editingId === t.id;

            const actions: HoverAction[] = [
              {
                icon: t.is_pinned ? PinOff : Pin,
                label: t.is_pinned ? "取消置顶" : "置顶",
                onClick: () => {
                  void pinThread(t.id, !t.is_pinned);
                },
              },
              {
                icon: Pencil,
                label: "重命名",
                onClick: () => {
                  setEditingId(t.id);
                  startEdit(t.name);
                },
              },
              {
                icon: Trash2,
                label: "删除",
                variant: "destructive",
                onClick: () => void onDelete(t.id),
              },
            ];

            return (
              <SidebarSessionRow
                key={t.id}
                selected={active}
                leading={
                  <>
                    <ThreadSourceIcon
                      backendKind={t.backend_kind}
                      claudeThreadId={t.claude_thread_id}
                      codexThreadId={t.codex_thread_id}
                      active={active}
                    />
                    <PhaseIndicator
                      phase={statuses[t.id]?.phase}
                      toolName={statuses[t.id]?.toolName}
                    />
                  </>
                }
                title={t.name || "未命名"}
                actions={actions}
                editing={isEditing}
                editSlot={
                  <input
                    {...inputProps}
                    className="h-9 flex-1 rounded-xl border border-border/80 bg-background/85 px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
                  />
                }
                onOpen={() => navigate(`/chat/${t.id}`)}
              />
            );
          })}
        </div>
      </div>
    </aside>
  );
}
