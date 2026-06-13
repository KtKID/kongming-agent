import { useState, type JSX } from "react";
import { useNavigate } from "react-router-dom";
import {
  Bot,
  Clock,
  Copy,
  FileCode2,
  GitBranch,
  LogOut,
  Menu,
  MessageSquare,
  Telescope,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSchedulerStore } from "@/modules/scheduler";
import { useSitian } from "@/modules/sitian/hooks/useSitian";
import { useAuthStore } from "@/stores/auth";
import { useWorkspaceStore, type WorkspaceTab } from "@/stores/workspace";

interface MobileToolsMenuProps {
  threadId?: string;
  threadTitle?: string;
}

export function MobileToolsMenu({
  threadId,
  threadTitle,
}: MobileToolsMenuProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const logout = useAuthStore((s) => s.logout);
  const setActiveTab = useWorkspaceStore((s) => s.setActiveTab);
  const openDrawer = useSchedulerStore((s) => s.openDrawer);
  const { open: openSitian } = useSitian();

  const activateTab = (tab: WorkspaceTab) => {
    if (!threadId) return;
    setActiveTab(threadId, tab);
    navigate(`/chat/${threadId}`);
    setOpen(false);
  };

  const copyThreadId = async () => {
    if (!threadId) return;
    try {
      await navigator.clipboard?.writeText(threadId);
      toast.success("Copied thread ID");
      setOpen(false);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      toast.error(`Copy failed: ${message}`);
    }
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-label="Open tools menu"
          className="gap-1.5 px-3"
        >
          <Menu className="h-3.5 w-3.5" />
          工具
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56 rounded-2xl p-2">
        <DropdownMenuLabel className="px-2 py-1 text-xs text-muted-foreground">
          {threadTitle?.trim() || "No active thread"}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={!threadId}
          onClick={() => activateTab("chat")}
          className="rounded-xl"
        >
          <MessageSquare className="h-4 w-4" />
          Chat
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!threadId}
          onClick={() => activateTab("files")}
          className="rounded-xl"
        >
          <FileCode2 className="h-4 w-4" />
          Files
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!threadId}
          onClick={() => activateTab("git")}
          className="rounded-xl"
        >
          <GitBranch className="h-4 w-4" />
          Git
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!threadId}
          onClick={() => activateTab("shell")}
          className="rounded-xl"
        >
          <Bot className="h-4 w-4" />
          Shell
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            openDrawer();
            setOpen(false);
          }}
          className="rounded-xl"
        >
          <Clock className="h-4 w-4" />
          定时任务
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={() => {
            void openSitian();
            setOpen(false);
          }}
          className="rounded-xl"
        >
          <Telescope className="h-4 w-4" />
          司天报告
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!threadId}
          onClick={() => {
            void copyThreadId();
          }}
          className="rounded-xl"
        >
          <Copy className="h-4 w-4" />
          复制线程 ID
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            void logout();
            setOpen(false);
          }}
          className="rounded-xl text-destructive focus:text-destructive"
        >
          <LogOut className="h-4 w-4" />
          退出登录
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
