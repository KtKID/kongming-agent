import type { JSX } from "react";
import { Bot, FileCode2, GitBranch, MessageSquare } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { WorkspaceTab } from "@/stores/workspace";

interface WorkspaceTabsProps {
  active: WorkspaceTab;
  onChange: (tab: WorkspaceTab) => void;
  disabled?: boolean;
}

export function WorkspaceTabs({
  active,
  onChange,
  disabled = false,
}: WorkspaceTabsProps): JSX.Element {
  return (
    <Tabs
      value={active}
      onValueChange={(value) => onChange(value as WorkspaceTab)}
      className="w-full"
    >
      <TabsList className="grid w-full grid-cols-4">
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
