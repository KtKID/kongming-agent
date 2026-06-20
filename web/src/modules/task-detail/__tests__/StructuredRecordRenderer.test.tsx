import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StructuredRecordRenderer } from "../renderers/StructuredRecordRenderer";

describe("StructuredRecordRenderer", () => {
  it("renders message content as markdown with visual line breaks", () => {
    render(
      <StructuredRecordRenderer
        index={0}
        record={{
          message: {
            role: "user",
            content: "# 标题\n正文第一行\n正文第二行",
          },
          created_at: "2026-06-19T00:00:00Z",
          usage: { total_tokens: 3 },
        }}
      />,
    );

    expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(screen.getByText(/正文第一行/)).toBeInTheDocument();
    expect(screen.getByText(/正文第二行/)).toBeInTheDocument();
    expect(screen.getByText("message.role")).toBeInTheDocument();
    expect(screen.getByText("created_at")).toBeInTheDocument();
    expect(screen.getByText("usage")).toBeInTheDocument();
  });

  it("renders parse error records without dropping the raw line", () => {
    render(
      <StructuredRecordRenderer
        index={1}
        record={{
          __parse_error__: true,
          line: 2,
          raw: "{bad json}",
          error: "Expected property name",
        }}
      />,
    );

    expect(screen.getByText("parse error line 2")).toBeInTheDocument();
    expect(screen.getByText("{bad json}")).toBeInTheDocument();
  });
});
