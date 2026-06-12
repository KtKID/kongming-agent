import { useEffect, useRef, useState } from "react";
import { apiGet } from "@/lib/api";

export interface SlashCandidate {
  slash: string;
  title: string;
  description: string;
  source: "command" | "skill";
}

interface SlashMenuProps {
  query: string;
  onSelect: (candidate: SlashCandidate) => void;
  onClose: () => void;
  visible: boolean;
  activeIndex: number;
  onFilteredChange: (filtered: SlashCandidate[]) => void;
}

let cachedCandidates: SlashCandidate[] | null = null;

async function fetchCandidates(): Promise<SlashCandidate[]> {
  if (cachedCandidates) return cachedCandidates;
  try {
    const data = await apiGet<SlashCandidate[]>("/api/slash-candidates");
    cachedCandidates = data;
    return data;
  } catch {
    return [];
  }
}

export function SlashMenu({
  query,
  onSelect,
  onClose,
  visible,
  activeIndex,
  onFilteredChange,
}: SlashMenuProps) {
  const [candidates, setCandidates] = useState<SlashCandidate[]>([]);
  const [filtered, setFiltered] = useState<SlashCandidate[]>([]);
  const menuRef = useRef<HTMLDivElement>(null);
  // 父组件常以匿名函数传入 onFilteredChange，每次渲染都是新引用。
  // 用 ref 持有最新值，避免它进 useEffect 依赖触发死循环。
  const onFilteredChangeRef = useRef(onFilteredChange);
  onFilteredChangeRef.current = onFilteredChange;

  useEffect(() => {
    if (!visible) return;
    fetchCandidates().then(setCandidates);
  }, [visible]);

  useEffect(() => {
    const q = query.toLowerCase();
    const result = candidates.filter(
      (c) =>
        c.slash.toLowerCase().includes(q) ||
        c.title.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q),
    );
    setFiltered(result);
    onFilteredChangeRef.current(result);
  }, [query, candidates]);

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

  if (!visible || filtered.length === 0) return null;

  return (
    <div
      ref={menuRef}
      className="absolute bottom-full left-0 z-50 mb-1 max-h-64 w-full overflow-y-auto rounded-lg border border-border bg-popover shadow-lg"
    >
      {filtered.map((c, i) => (
        <button
          key={c.slash}
          type="button"
          className={[
            "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition-colors",
            i === activeIndex
              ? "bg-accent text-accent-foreground"
              : "hover:bg-accent/50",
          ].join(" ")}
          onMouseDown={(e) => {
            e.preventDefault();
            onSelect(c);
          }}
        >
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
            {c.slash}
          </span>
          <span className="truncate text-foreground">{c.title}</span>
          <span className="ml-auto truncate text-xs text-muted-foreground">
            {c.description}
          </span>
        </button>
      ))}
    </div>
  );
}

export function useSlashMenu() {
  const [showMenu, setShowMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");

  const handleInputChange = (text: string) => {
    if (
      text === "/" ||
      (text.startsWith("/") && !text.includes(" ") && !text.includes("\n"))
    ) {
      setShowMenu(true);
      setSlashQuery(text.slice(1));
    } else {
      setShowMenu(false);
      setSlashQuery("");
    }
  };

  return { showMenu, slashQuery, setShowMenu, handleInputChange };
}
