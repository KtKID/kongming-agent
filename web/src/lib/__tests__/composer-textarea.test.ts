import { describe, expect, it } from "vitest";
import {
  COMPOSER_TEXTAREA_MAX_ROWS,
  COMPOSER_TEXTAREA_MIN_ROWS,
  getComposerTextareaHeightBounds,
} from "@/lib/composer-textarea";

describe("composer textarea bounds", () => {
  it("derives two-line min height and six-line max height", () => {
    const bounds = getComposerTextareaHeightBounds({
      lineHeight: "20px",
      paddingTop: "8px",
      paddingBottom: "8px",
      borderTopWidth: "1px",
      borderBottomWidth: "1px",
    } as CSSStyleDeclaration);

    expect(bounds).toEqual({
      minHeight: 20 * COMPOSER_TEXTAREA_MIN_ROWS + 18,
      maxHeight: 20 * COMPOSER_TEXTAREA_MAX_ROWS + 18,
    });
  });
});
