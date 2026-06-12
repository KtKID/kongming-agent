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
      className="flex w-full min-w-0 flex-col gap-2 lg:flex-row lg:items-center lg:gap-3"
    >
      {threadId && (
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/60 bg-background/38 px-3 py-2 shadow-sm lg:mr-auto">
          <span className="truncate font-mono text-[11px] text-muted-foreground" title={threadId}>
            {threadId}
          </span>
          <button
            type="button"
            aria-label="Copy ID"
            title="Copy ID"
            className="shrink-0 rounded-lg p-1 text-muted-foreground/60 transition-colors hover:bg-secondary hover:text-muted-foreground"
            onClick={handleCopy}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          </button>
        </div>
      )}
      <TabsList className="inline-flex w-full shrink-0 justify-between lg:ml-auto lg:w-auto lg:justify-start">
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
