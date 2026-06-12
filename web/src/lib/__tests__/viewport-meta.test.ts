import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("index viewport meta", () => {
  it("locks page scale and keeps keyboard resize hint", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

    expect(html).toContain('name="viewport"');
    expect(html).toContain("maximum-scale=1.0");
    expect(html).toContain("user-scalable=no");
    expect(html).toContain("interactive-widget=resizes-content");
  });
});
