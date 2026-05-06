import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { WorkspaceFilesPanel } from "@/components/WorkspaceFilesPanel";
import { WorkspaceShellPanel } from "@/components/WorkspaceShellPanel";
import type {
  WorkspaceContextDTO,
  WorkspaceFileDTO,
  WorkspaceTreeDTO,
} from "@/protocol";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse(): {
  promise: Promise<Response>;
  resolve: (response: Response) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (response: Response) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<Response>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const claudeContext: WorkspaceContextDTO = {
  thread_id: "thread-1",
  backend_kind: "claude_code",
  workspace_root: "/tmp/proj",
  claude_thread_id: "sdk-1",
  shell_provider: "claude_code",
  files_available: true,
  shell_available: true,
  unavailable_reason: null,
};

describe("Workspace panels", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("Files 面板在可用时显示 workspaceRoot", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResponse({ path: "", entries: [] }),
    ) as unknown as typeof fetch;

    render(<WorkspaceFilesPanel context={claudeContext} />);

    expect(screen.getByTestId("workspace-files-panel")).toBeInTheDocument();
    expect(await screen.findByText("/tmp/proj")).toBeInTheDocument();
  });

  it("Shell 面板在不可用时显示原因", () => {
    render(
      <WorkspaceShellPanel
        context={{
          thread_id: "thread-1",
          backend_kind: "generic_chat",
          workspace_root: "",
          claude_thread_id: "",
          shell_provider: "none",
          files_available: false,
          shell_available: false,
          unavailable_reason: "thread has no workspace cwd",
        }}
      />,
    );
    expect(screen.getByText("Workspace Shell 当前不可用")).toBeInTheDocument();
    expect(screen.getByText("thread has no workspace cwd")).toBeInTheDocument();
  });

  it("搜索会 trim + lowercase，并在命中后自动展开祖先目录，清空后恢复完整树", async () => {
    globalThis.fetch = vi.fn((input) => {
      const url = String(input);
      if (url.includes("workspace-tree?path=")) {
        const path = decodeURIComponent(url.split("path=")[1] ?? "");
        if (path === "") {
          return Promise.resolve(
            jsonResponse({
              path: "",
              entries: [
                { path: "README.md", name: "README.md", kind: "file", has_children: false },
                { path: "src", name: "src", kind: "dir", has_children: true },
              ],
            } satisfies WorkspaceTreeDTO),
          );
        }
        if (path === "src") {
          return Promise.resolve(
            jsonResponse({
              path: "src",
              entries: [
                { path: "src/components", name: "components", kind: "dir", has_children: true },
                { path: "src/utils.ts", name: "utils.ts", kind: "file", has_children: false },
              ],
            } satisfies WorkspaceTreeDTO),
          );
        }
        if (path === "src/components") {
          return Promise.resolve(
            jsonResponse({
              path: "src/components",
              entries: [
                {
                  path: "src/components/Panel.tsx",
                  name: "Panel.tsx",
                  kind: "file",
                  has_children: false,
                },
              ],
            } satisfies WorkspaceTreeDTO),
          );
        }
      }
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceFilesPanel context={claudeContext} />);

    expect(await screen.findByText("README.md")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "src" }));
    expect(await screen.findByText("components")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "components" }));
    expect(await screen.findByText("Panel.tsx")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "components" }));
    await waitFor(() => {
      expect(screen.queryByText("Panel.tsx")).toBeNull();
    });
    fireEvent.click(screen.getByRole("button", { name: "src" }));
    await waitFor(() => {
      expect(screen.queryByText("components")).toBeNull();
    });

    fireEvent.change(screen.getByPlaceholderText("搜索文件"), {
      target: { value: "  PANEL  " },
    });

    expect(await screen.findByText("Panel.tsx")).toBeInTheDocument();
    expect(screen.getByText("src")).toBeInTheDocument();
    expect(screen.getByText("components")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("搜索文件"), {
      target: { value: "" },
    });

    await waitFor(() => {
      expect(screen.getByText("README.md")).toBeInTheDocument();
      expect(screen.getByText("src")).toBeInTheDocument();
      expect(screen.queryByText("components")).toBeNull();
      expect(screen.queryByText("Panel.tsx")).toBeNull();
    });
  });

  it("会忽略旧 thread 的迟到 tree 响应", async () => {
    const thread1Root = deferredResponse();
    const thread2Root = deferredResponse();

    globalThis.fetch = vi.fn((input) => {
      const url = String(input);
      if (url.includes("/api/threads/thread-1/workspace-tree")) return thread1Root.promise;
      if (url.includes("/api/threads/thread-2/workspace-tree")) return thread2Root.promise;
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    const { rerender } = render(<WorkspaceFilesPanel context={claudeContext} />);

    rerender(
      <WorkspaceFilesPanel
        context={{
          ...claudeContext,
          thread_id: "thread-2",
          workspace_root: "/tmp/proj-2",
        }}
      />,
    );

    thread2Root.resolve(
      jsonResponse({
        path: "",
        entries: [{ path: "b.txt", name: "b.txt", kind: "file", has_children: false }],
      } satisfies WorkspaceTreeDTO),
    );
    expect(await screen.findByText("b.txt")).toBeInTheDocument();

    thread1Root.resolve(
      jsonResponse({
        path: "",
        entries: [{ path: "a.txt", name: "a.txt", kind: "file", has_children: false }],
      } satisfies WorkspaceTreeDTO),
    );

    await waitFor(() => {
      expect(screen.getByText("b.txt")).toBeInTheDocument();
      expect(screen.queryByText("a.txt")).toBeNull();
    });
  });

  it("会忽略旧文件请求的迟到响应", async () => {
    const alphaFile = deferredResponse();
    const betaFile = deferredResponse();

    globalThis.fetch = vi.fn((input) => {
      const url = String(input);
      if (url.includes("workspace-tree?path=")) {
        return Promise.resolve(
          jsonResponse({
            path: "",
            entries: [
              { path: "alpha.txt", name: "alpha.txt", kind: "file", has_children: false },
              { path: "beta.txt", name: "beta.txt", kind: "file", has_children: false },
            ],
          } satisfies WorkspaceTreeDTO),
        );
      }
      if (url.includes("workspace-file?path=alpha.txt")) return alphaFile.promise;
      if (url.includes("workspace-file?path=beta.txt")) return betaFile.promise;
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceFilesPanel context={claudeContext} />);

    fireEvent.click(await screen.findByRole("button", { name: "alpha.txt" }));
    fireEvent.click(screen.getByRole("button", { name: "beta.txt" }));

    betaFile.resolve(
      jsonResponse({
        path: "beta.txt",
        name: "beta.txt",
        content: "beta",
        size_bytes: 4,
        is_text: true,
        too_large: false,
        encoding: "utf-8",
      } satisfies WorkspaceFileDTO),
    );
    expect(await screen.findByDisplayValue("beta")).toBeInTheDocument();

    alphaFile.resolve(
      jsonResponse({
        path: "alpha.txt",
        name: "alpha.txt",
        content: "alpha",
        size_bytes: 5,
        is_text: true,
        too_large: false,
        encoding: "utf-8",
      } satisfies WorkspaceFileDTO),
    );

    await waitFor(() => {
      expect(screen.getByDisplayValue("beta")).toBeInTheDocument();
      expect(screen.queryByDisplayValue("alpha")).toBeNull();
    });
  });

  it("保存失败后保留草稿", async () => {
    globalThis.fetch = vi.fn((input, init) => {
      const url = String(input);
      if (url.includes("workspace-tree?path=")) {
        return Promise.resolve(
          jsonResponse({
            path: "",
            entries: [{ path: "note.txt", name: "note.txt", kind: "file", has_children: false }],
          } satisfies WorkspaceTreeDTO),
        );
      }
      if (url.includes("workspace-file?path=note.txt") && (!init || init.method === "GET")) {
        return Promise.resolve(
          jsonResponse({
            path: "note.txt",
            name: "note.txt",
            content: "original",
            size_bytes: 8,
            is_text: true,
            too_large: false,
            encoding: "utf-8",
          } satisfies WorkspaceFileDTO),
        );
      }
      if (url.endsWith("/workspace-file") && init?.method === "PUT") {
        return Promise.resolve(
          jsonResponse(
            {
              error_code: "internal",
              message: "save failed",
            },
            500,
          ),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    }) as unknown as typeof fetch;

    render(<WorkspaceFilesPanel context={claudeContext} />);

    fireEvent.click(await screen.findByRole("button", { name: "note.txt" }));
    const editor = await screen.findByDisplayValue("original");
    fireEvent.change(editor, { target: { value: "edited draft" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText("[internal] save failed")).toBeInTheDocument();
    expect(screen.getByDisplayValue("edited draft")).toBeInTheDocument();
  });
});
