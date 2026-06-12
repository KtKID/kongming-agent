import { describe, expect, it } from "vitest";
import {
  buildWhiteboardCardDraft,
  shouldStartWhiteboardCardInEditor,
} from "@/lib/whiteboard-card-templates";

describe("whiteboard-card-templates", () => {
  it("builds a blank note draft", () => {
    expect(buildWhiteboardCardDraft("note", 2)).toEqual({
      title: "便签 2",
      category: "note",
      content: "",
      height: 200,
    });
  });

  it("builds a single-line todo draft", () => {
    expect(buildWhiteboardCardDraft("todo", 3)).toEqual({
      title: "待办 3",
      category: "todo",
      content: "- [ ] ",
      height: 220,
    });
  });

  it("starts blank notes and stub todos in editor mode", () => {
    expect(shouldStartWhiteboardCardInEditor("")).toBe(true);
    expect(shouldStartWhiteboardCardInEditor("- [ ] ")).toBe(true);
    expect(shouldStartWhiteboardCardInEditor("- [ ] 下一步")).toBe(false);
  });
});
