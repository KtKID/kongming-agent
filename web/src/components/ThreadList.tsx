import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { MessageSquare, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Input } from "@/components/ui/input";
import { useThreadsStore } from "@/stores/threads";
import { NewThreadDialog } from "@/components/NewThreadDialog";
import { cn } from "@/lib/utils";
import type { BackendKind } from "@/protocol";

const THREAD_SOURCE_META: Record<
  BackendKind,
  {
    label: string;
    Icon?: typeof MessageSquare;
    imageSrc?: string;
  }
> = {
  generic_chat: { label: "普通聊天", Icon: MessageSquare },
  claude_code: { label: "Claude", imageSrc: "/brand/claude-app-icon.png" },
  codex: { label: "Codex", imageSrc: "/brand/codex-app-icon.png" },
};

/**
 * 左侧 thread 列表：
 * - updated_at 倒序（store 自动排序）
 * - 选中高亮（参数 thread_id）
 * - rename 内联编辑 + delete dropdown
 * - "+" 按钮 → NewThreadDialog
 */
export function ThreadList() {
  const params = useParams<{ thread_id?: string }>();
  const navigate = useNavigate();
  const threads = useThreadsStore((s) => s.threads);
  const fetchThreads = useThreadsStore((s) => s.fetchThreads);
  const fetchPresets = useThreadsStore((s) => s.fetchPresets);
  const renameThread = useThreadsStore((s) => s.renameThread);
  const deleteThread = useThreadsStore((s) => s.deleteThread);
  const [newOpen, setNewOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  useEffect(() => {
    void fetchThreads();
    void fetchPresets();
  }, [fetchThreads, fetchPresets]);

  const onRename = async (id: string) => {
    const name = editName.trim();
    if (!name) {
      setEditingId(null);
      return;
    }
    try {
      await renameThread(id, name);
      toast.success("已重命名");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`重命名失败：${msg}`);
    } finally {
      setEditingId(null);
    }
  };

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
    <aside className="flex h-full w-80 shrink-0 flex-col border-r border-border bg-card">
      <div className="flex items-center justify-between border-b border-border p-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          会话
        </span>
        <Button
          size="icon"
          variant="ghost"
          onClick={() => setNewOpen(true)}
          aria-label="新建会话"
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
      <ScrollArea className="flex-1">
        <div className="flex flex-col gap-0.5 p-2">
          {threads.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              还没有会话
            </div>
          ) : null}
          {threads.map((t) => {
            const active = params.thread_id === t.id;
            const sourceMeta = THREAD_SOURCE_META[t.backend_kind];
            return (
              <div
                key={t.id}
                className={cn(
                  "group flex items-center gap-2 rounded-md px-2.5 py-2 text-sm transition-colors",
                  active
                    ? "bg-secondary text-foreground"
                    : "hover:bg-secondary/60",
                )}
              >
                {editingId === t.id ? (
                  <Input
                    autoFocus
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    onBlur={() => void onRename(t.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void onRename(t.id);
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    className="h-7"
                  />
                ) : (
                  <Link
                    to={`/chat/${t.id}`}
                    className="flex min-w-0 flex-1 items-center gap-2 truncate"
                    title={t.name}
                  >
                    <span
                      aria-label={`${sourceMeta.label} 会话`}
                      title={sourceMeta.label}
                      className={cn(
                        "flex h-5 w-5 shrink-0 items-center justify-center rounded-sm border border-border/60 bg-background/60 text-muted-foreground",
                        active && "border-border bg-background text-foreground/80",
                      )}
                    >
                      {sourceMeta.imageSrc ? (
                        <img
                          src={sourceMeta.imageSrc}
                          alt=""
                          aria-hidden="true"
                          className="h-4 w-4 rounded-[4px] object-cover"
                        />
                      ) : sourceMeta.Icon ? (
                        <sourceMeta.Icon className="h-3.5 w-3.5" aria-hidden="true" />
                      ) : null}
                    </span>
                    <span className="truncate">{t.name || "未命名"}</span>
                  </Link>
                )}
                <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                  <button
                    type="button"
                    aria-label="重命名"
                    title="重命名"
                    className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setEditingId(t.id);
                      setEditName(t.name);
                    }}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label="删除"
                    title="删除"
                    className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      void onDelete(t.id);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </ScrollArea>
      <NewThreadDialog open={newOpen} onOpenChange={setNewOpen} />
    </aside>
  );
}
