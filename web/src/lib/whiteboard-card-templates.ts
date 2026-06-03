export type WhiteboardCardKind = "note" | "todo";

interface WhiteboardCardDraft {
  title: string;
  category: string;
  content: string;
  height: number;
}

const QUICK_TODO_RE = /^-\s+\[(?: |x|X)\]\s*$/;

export function buildWhiteboardCardDraft(
  kind: WhiteboardCardKind,
  sequence: number,
): WhiteboardCardDraft {
  if (kind === "todo") {
    return {
      title: `待办 ${sequence}`,
      category: "todo",
      content: "- [ ] ",
      height: 220,
    };
  }

  return {
    title: `便签 ${sequence}`,
    category: "note",
    content: "",
    height: 200,
  };
}

export function shouldStartWhiteboardCardInEditor(content: string): boolean {
  const trimmed = content.trim();
  return trimmed.length === 0 || QUICK_TODO_RE.test(trimmed);
}
