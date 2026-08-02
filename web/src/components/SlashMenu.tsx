import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  History,
  Loader2,
  Sparkles,
  Terminal,
  Workflow,
} from "lucide-react";
import {
  fetchSlashCatalogItems,
  type SlashCatalogItem,
} from "@/lib/slash-catalog";

export type { SlashCatalogItem };

export type SlashMenuEntry =
  | { type: "item"; id: string; item: SlashCatalogItem }
  | {
      type: "status";
      id: string;
      title: string;
      description: string;
      tone: "loading" | "empty" | "error";
    };

interface SlashMenuProps {
  entries: SlashMenuEntry[];
  onActivate: (entry: SlashMenuEntry) => void;
  onClose: () => void;
  visible: boolean;
  activeIndex: number;
}

export function SlashMenu({
  entries,
  onActivate,
  onClose,
  visible,
  activeIndex,
}: SlashMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const activeEntryRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!visible) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [visible, onClose]);

  useEffect(() => {
    if (!visible) return;
    activeEntryRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, visible, entries]);

  if (!visible || entries.length === 0) return null;

  return (
    <div
      ref={menuRef}
      data-testid="slash-menu"
      className="absolute bottom-full left-0 z-50 mb-1 max-h-64 w-full overflow-y-auto rounded-lg border border-border bg-popover shadow-lg"
    >
      {entries.map((entry, i) => (
        <button
          key={entry.id}
          ref={i === activeIndex ? activeEntryRef : null}
          type="button"
          disabled={entry.type === "status" || !entry.item.enabled}
          className={[
            "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition-colors",
            i === activeIndex
              ? "bg-accent text-accent-foreground"
              : "hover:bg-accent/50",
            (entry.type === "status" || !entry.item.enabled) &&
              "cursor-default opacity-75 hover:bg-transparent",
          ]
            .filter(Boolean)
            .join(" ")}
          onMouseDown={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onActivate(entry);
          }}
        >
          <EntryIcon entry={entry} />
          <span className="min-w-0 flex-1 truncate text-foreground">
            {entryTitle(entry)}
          </span>
          {entry.type === "item" && (
            <span className="shrink-0 rounded border border-border/70 bg-muted/60 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {itemKindLabel(entry.item)}
            </span>
          )}
          <span className="ml-auto min-w-0 max-w-[55%] shrink truncate text-xs text-muted-foreground">
            {entryDescription(entry)}
          </span>
        </button>
      ))}
    </div>
  );
}

export function useSlashMenu(threadId?: string) {
  const [showMenu, setShowMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [items, setItems] = useState<SlashCatalogItem[]>([]);
  const [catalogThreadId, setCatalogThreadId] = useState(threadId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const closeMenu = () => {
    setShowMenu(false);
    setSlashQuery("");
  };

  const handleInputChange = (text: string) => {
    if (
      text === "/" ||
      (text.startsWith("/") && !text.includes(" ") && !text.includes("\n"))
    ) {
      setShowMenu(true);
      setSlashQuery(text.slice(1));
      return;
    }
    closeMenu();
  };

  useEffect(() => {
    if (!showMenu) return;
    let active = true;
    setCatalogThreadId(threadId);
    setLoading(true);
    setError(null);
    fetchSlashCatalogItems(threadId)
      .then((nextItems) => {
        if (!active) return;
        setItems(nextItems);
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
        setItems([]);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [showMenu, threadId]);

  const entries = useMemo(
    () =>
      buildEntries({
        items,
        loading,
        error,
        query: slashQuery,
        catalogIsCurrent: catalogThreadId === threadId,
      }),
    [items, loading, error, slashQuery, catalogThreadId, threadId],
  );

  const activateEntry = (entry: SlashMenuEntry): SlashCatalogItem | null => {
    if (entry.type === "item" && entry.item.enabled) {
      return entry.item;
    }
    return null;
  };

  return {
    showMenu,
    entries,
    handleInputChange,
    activateEntry,
    closeMenu,
  };
}

function buildEntries({
  items,
  loading,
  error,
  query,
  catalogIsCurrent,
}: {
  items: SlashCatalogItem[];
  loading: boolean;
  error: string | null;
  query: string;
  catalogIsCurrent: boolean;
}): SlashMenuEntry[] {
  if (!catalogIsCurrent) return [];
  if (loading) {
    return [
      {
        type: "status",
        id: "__loading",
        title: "Loading",
        description: "",
        tone: "loading",
      },
    ];
  }
  if (error) {
    return [
      {
        type: "status",
        id: "__error",
        title: "Load failed",
        description: error,
        tone: "error",
      },
    ];
  }
  const filteredItems = searchItems(items, query);
  if (filteredItems.length === 0) {
    return [
      {
        type: "status",
        id: "__empty",
        title: "No matching actions",
        description: "",
        tone: "empty",
      },
    ];
  }
  return filteredItems.map((item) => ({
    type: "item" as const,
    id: `item:${item.id}`,
    item,
  }));
}

function searchItems(
  items: SlashCatalogItem[],
  rawQuery: string,
): SlashCatalogItem[] {
  const query = normalizeSearchText(rawQuery);
  if (!query) return [...items].sort(compareBaseOrder);
  return items
    .map((item) => ({ item, score: scoreItem(item, query) }))
    .filter((candidate) => candidate.score >= 0)
    .sort(
      (left, right) =>
        right.score - left.score || compareBaseOrder(left.item, right.item),
    )
    .map((candidate) => candidate.item);
}

function scoreItem(item: SlashCatalogItem, query: string): number {
  const primaryFields = [
    item.slash ?? "",
    item.title,
    item.id,
    item.section_id ?? "",
    String(item.metadata.mode ?? ""),
    String(item.metadata.workflow_id ?? ""),
  ];
  let bestScore = -1;
  primaryFields.forEach((field, index) => {
    const score = scoreField(field, query);
    if (score >= 0) bestScore = Math.max(bestScore, score - index * 10);
  });
  const descriptionScore = scoreField(item.description, query, false);
  if (descriptionScore >= 0) {
    bestScore = Math.max(bestScore, descriptionScore - 200);
  }
  return bestScore;
}

function scoreField(
  rawValue: string,
  query: string,
  allowOrderedSubsequence = true,
): number {
  const value = normalizeSearchText(rawValue);
  if (!value) return -1;
  if (value === query) return 1000;
  if (value.startsWith(query)) return 800 - (value.length - query.length);
  const substringIndex = value.indexOf(query);
  if (substringIndex >= 0) return 600 - substringIndex;
  if (!allowOrderedSubsequence) return -1;
  return scoreOrderedSubsequence(value, query);
}

function scoreOrderedSubsequence(value: string, query: string): number {
  let queryIndex = 0;
  let firstMatch = -1;
  let lastMatch = -1;
  let gapCount = 0;
  for (let valueIndex = 0; valueIndex < value.length; valueIndex += 1) {
    if (value[valueIndex] !== query[queryIndex]) continue;
    if (firstMatch < 0) firstMatch = valueIndex;
    if (lastMatch >= 0) gapCount += valueIndex - lastMatch - 1;
    lastMatch = valueIndex;
    queryIndex += 1;
    if (queryIndex === query.length) {
      return 400 - firstMatch * 2 - gapCount;
    }
  }
  return -1;
}

function normalizeSearchText(value: string): string {
  return value.normalize("NFKC").trim().replace(/^\/+/, "").toLowerCase();
}

function compareBaseOrder(
  left: SlashCatalogItem,
  right: SlashCatalogItem,
): number {
  return (
    itemKindPriority(left) - itemKindPriority(right) ||
    left.order - right.order ||
    left.title.localeCompare(right.title)
  );
}

function itemKindPriority(item: SlashCatalogItem): number {
  if (item.kind === "command") return 0;
  if (item.kind === "skill") return 1;
  if (item.kind === "workflow_strategy") return 2;
  return 3;
}

function entryTitle(entry: SlashMenuEntry): string {
  if (entry.type === "item") return entry.item.slash ?? entry.item.title;
  return entry.title;
}

function entryDescription(entry: SlashMenuEntry): string {
  if (entry.type === "item") return entry.item.description;
  return entry.description;
}

function itemKindLabel(item: SlashCatalogItem): string {
  if (item.kind === "command") return "Command";
  if (item.kind === "skill") return "Skill";
  return "Workflow";
}

function EntryIcon({ entry }: { entry: SlashMenuEntry }) {
  if (entry.type === "status" && entry.tone === "loading") {
    return <Loader2 className="h-4 w-4 shrink-0 animate-spin" />;
  }
  if (entry.type === "status" && entry.tone === "error") {
    return <AlertCircle className="h-4 w-4 shrink-0 text-destructive" />;
  }
  if (entry.type === "status") {
    return <Sparkles className="h-4 w-4 shrink-0 opacity-70" />;
  }
  if (entry.item.kind === "workflow_run") return <History className="h-4 w-4 shrink-0" />;
  if (entry.item.kind === "command") return <Terminal className="h-4 w-4 shrink-0" />;
  if (entry.item.kind === "workflow_strategy") return <Workflow className="h-4 w-4 shrink-0" />;
  return <Sparkles className="h-4 w-4 shrink-0" />;
}
