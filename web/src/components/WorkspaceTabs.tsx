import { useState, type JSX } from "react";
import { Bot, Check, Copy, FileCode2, GitBranch, MessageSquare } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { WorkspaceTab } from "@/stores/workspace";

interface WorkspaceTabsProps {
  active: WorkspaceTab;
  onChange: (tab: WorkspaceTab) => void;
  disabled?: boolean;
  threadId?: string;
}

export function WorkspaceTabs({
  active,
  onChange,
  disabled = false,
  threadId,
}: WorkspaceTabsProps): JSX.Element {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (!threadId) return;
    void navigator.clipboard.writeText(threadId).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <Tabs
      value={active}
      onValueChange={(value) => onChange(value as WorkspaceTab)}
      className="flex items-center"
    >
      {threadId && (
        <div className="mr-auto flex min-w-0 items-center gap-1 pl-1">
          <span className="truncate text-xs text-muted-foreground" title={threadId}>
            {threadId}
          </span>
          <button
            type="button"
            aria-label="复制 ID"
            title="复制 ID"
            className="shrink-0 rounded p-0.5 text-muted-foreground/60 hover:text-muted-foreground"
            onClick={handleCopy}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          </button>
        </div>
      )}
      <TabsList className="inline-flex w-auto shrink-0">
        <TabsTrigger value="chat" className="gap-1.5" disabled={disabled}>
          <MessageSquare className="h-3.5 w-3.5" />
          Chat
        </TabsTrigger>
        <TabsTrigger value="files" className="gap-1.5" disabled={disabled}>
          <FileCode2 className="h-3.5 w-3.5" />
          Files
        </TabsTrigger>
        <TabsTrigger value="git" className="gap-1.5" disabled={disabled}>
          <GitBranch className="h-3.5 w-3.5" />
          Git
        </TabsTrigger>
        <TabsTrigger value="shell" className="gap-1.5" disabled={disabled}>
          <Bot className="h-3.5 w-3.5" />
          Shell
        </TabsTrigger>
      </TabsList>
    </Tabs>
  );
}
