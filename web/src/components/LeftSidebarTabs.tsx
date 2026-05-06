import type { JSX } from "react";
import { MessageSquare, Bot, Terminal } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export type LeftSidebarTab = "generic" | "claude" | "codex";

interface LeftSidebarTabsProps {
  active: LeftSidebarTab;
  onChange: (tab: LeftSidebarTab) => void;
}

const STORAGE_KEY = "kongming.left-sidebar-tab";

export function loadPersistedTab(): LeftSidebarTab {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "claude" || v === "codex") return v;
    return "generic";
  } catch {
    return "generic";
  }
}

export function persistTab(tab: LeftSidebarTab): void {
  try {
    localStorage.setItem(STORAGE_KEY, tab);
  } catch {
    // ignore
  }
}

export function LeftSidebarTabs({ active, onChange }: LeftSidebarTabsProps): JSX.Element {
  return (
    <Tabs
      value={active}
      onValueChange={(v) => onChange(v as LeftSidebarTab)}
      className="w-full"
    >
      <TabsList className="grid w-full grid-cols-3">
        <TabsTrigger value="generic" className="gap-1">
          <MessageSquare className="h-3.5 w-3.5" />
          通用
        </TabsTrigger>
        <TabsTrigger value="claude" className="gap-1">
          <Bot className="h-3.5 w-3.5" />
          Claude
        </TabsTrigger>
        <TabsTrigger value="codex" className="gap-1">
          <Terminal className="h-3.5 w-3.5" />
          Codex
        </TabsTrigger>
      </TabsList>
    </Tabs>
  );
}
