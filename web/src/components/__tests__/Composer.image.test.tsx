/**
 * Composer 图片粘贴 + 上传链路单元测试（claude-image-paste-e2e #17）。
 *
 * 覆盖四块（共 24 用例）：
 *   1. usePasteAttachments hook：粘贴拦截行为（image / mixed / 文本 / disabled）
 *   2. useAttachmentUploader hook：状态机 + fetch 成功/失败 + 客户端预校验 + remove/clear
 *   3. ThumbnailStrip 组件：渲染三态 + ✕ 派发
 *   4. Composer 集成：粘贴 → 缩略图 → 发送 → 清空 + 边界（uploading 阻发送 / threadId 缺失 / disabled）
 *
 * 关键 mock：
 *   - 全局 fetch 用 vi.stubGlobal 注入，每个 case 控制 resolved value
 *   - URL.createObjectURL / revokeObjectURL 兜底（jsdom 不实现）
 *   - sonner toast 整体 mock，避免污染输出
 */

import {
  act,
  render,
  renderHook,
  screen,
  fireEvent,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";
import type { ClipboardEvent as ReactClipboardEvent } from "react";

import { Composer } from "@/components/Composer";
import { ThumbnailStrip } from "@/components/ThumbnailStrip";
import { useAttachmentUploader, type AttachmentState } from "@/hooks/useAttachmentUploader";
import { usePasteAttachments } from "@/hooks/usePasteAttachments";
import type { UserInputAttachment } from "@/protocol";

// ---------------------------------------------------------------------------
// 全局 mock
// ---------------------------------------------------------------------------

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

let createObjectURLSpy: Mock | undefined;
let revokeObjectURLSpy: Mock | undefined;

beforeAll(() => {
  // jsdom 没有 URL.createObjectURL；hook 通过 `URL.createObjectURL(file)` 生成 preview
  if (typeof URL.createObjectURL !== "function") {
    createObjectURLSpy = vi.fn(() => `blob:mock-${Math.random().toString(36).slice(2)}`);
    revokeObjectURLSpy = vi.fn();
    (URL as unknown as { createObjectURL: Mock }).createObjectURL = createObjectURLSpy;
    (URL as unknown as { revokeObjectURL: Mock }).revokeObjectURL = revokeObjectURLSpy;
  }
});

afterAll(() => {
  if (createObjectURLSpy) {
    delete (URL as unknown as { createObjectURL?: unknown }).createObjectURL;
    delete (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL;
  }
});

let fetchMock: Mock;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

function makeImageFile(
  name = "paste.png",
  mime = "image/png",
  sizeBytes = 1024,
): File {
  const blob = new Blob([new Uint8Array(sizeBytes)], { type: mime });
  return new File([blob], name, { type: mime });
}

function makeClipboardItem(file: File): DataTransferItem {
  return {
    kind: "file",
    type: file.type,
    getAsFile: () => file,
    getAsString: (_cb: (s: string) => void) => undefined,
    webkitGetAsEntry: () => null,
  } as unknown as DataTransferItem;
}

function makeTextClipboardItem(text: string, mime = "text/plain"): DataTransferItem {
  return {
    kind: "string",
    type: mime,
    getAsFile: () => null,
    getAsString: (cb: (s: string) => void) => cb(text),
    webkitGetAsEntry: () => null,
  } as unknown as DataTransferItem;
}

function makePasteEvent(
  items: DataTransferItem[],
): ReactClipboardEvent<HTMLTextAreaElement> {
  const preventDefault = vi.fn();
  return {
    preventDefault,
    clipboardData: {
      items: items as unknown as DataTransferItemList,
    },
  } as unknown as ReactClipboardEvent<HTMLTextAreaElement>;
}

function makeReadyAttachment(asset_id = "asset-1"): UserInputAttachment {
  return {
    asset_id,
    kind: "image",
    mime_type: "image/png",
    size_bytes: 1024,
    preview_url: `/api/uploads/${asset_id}`,
    status: "ready",
  };
}

/** 把 fetch 配置为成功响应一次，返回的 attachment.asset_id 可指定 */
function mockFetchOk(asset_id = "asset-1"): UserInputAttachment {
  const attachment = makeReadyAttachment(asset_id);
  fetchMock.mockResolvedValueOnce({
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => attachment,
  } as unknown as Response);
  return attachment;
}

/** 把 fetch 配置为失败响应一次（status + detail.code） */
function mockFetchFail(status: number, code: string, message = "x"): void {
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status,
    statusText: code,
    json: async () => ({ detail: { code, message } }),
  } as unknown as Response);
}

// ---------------------------------------------------------------------------
// §1 usePasteAttachments hook
// ---------------------------------------------------------------------------

describe("usePasteAttachments", () => {
  it("粘贴单张 PNG → onImagePaste 被调用 1 次且参数是 File", () => {
    const onImagePaste = vi.fn();
    const { result } = renderHook(() => usePasteAttachments({ onImagePaste }));
    const file = makeImageFile("a.png");
    const ev = makePasteEvent([makeClipboardItem(file)]);
    result.current.onPaste(ev);
    expect(onImagePaste).toHaveBeenCalledTimes(1);
    expect(onImagePaste).toHaveBeenCalledWith(file);
    expect(ev.preventDefault).toHaveBeenCalledTimes(1);
  });

  it("粘贴多张 mixed PNG+JPEG → onImagePaste 被调用 N 次，顺序对", () => {
    const onImagePaste = vi.fn();
    const { result } = renderHook(() => usePasteAttachments({ onImagePaste }));
    const png = makeImageFile("a.png", "image/png");
    const jpg = makeImageFile("b.jpg", "image/jpeg");
    const ev = makePasteEvent([
      makeClipboardItem(png),
      makeClipboardItem(jpg),
    ]);
    result.current.onPaste(ev);
    expect(onImagePaste).toHaveBeenCalledTimes(2);
    expect(onImagePaste.mock.calls[0][0]).toBe(png);
    expect(onImagePaste.mock.calls[1][0]).toBe(jpg);
  });

  it("粘贴纯文本 → onImagePaste 不被调用，preventDefault 不被调用", () => {
    const onImagePaste = vi.fn();
    const { result } = renderHook(() => usePasteAttachments({ onImagePaste }));
    const ev = makePasteEvent([makeTextClipboardItem("hello world")]);
    result.current.onPaste(ev);
    expect(onImagePaste).not.toHaveBeenCalled();
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it("disabled=true → 即使粘贴图片也不调 onImagePaste，也不 preventDefault", () => {
    const onImagePaste = vi.fn();
    const { result } = renderHook(() =>
      usePasteAttachments({ onImagePaste, disabled: true }),
    );
    const ev = makePasteEvent([makeClipboardItem(makeImageFile())]);
    result.current.onPaste(ev);
    expect(onImagePaste).not.toHaveBeenCalled();
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it("粘贴图片时 preventDefault 被调用", () => {
    const onImagePaste = vi.fn();
    const { result } = renderHook(() => usePasteAttachments({ onImagePaste }));
    const ev = makePasteEvent([makeClipboardItem(makeImageFile())]);
    result.current.onPaste(ev);
    expect(ev.preventDefault).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// §2 useAttachmentUploader hook
// ---------------------------------------------------------------------------

describe("useAttachmentUploader", () => {
  it("上传成功 → uploading → ready，attachment 字段填充", async () => {
    const expected = mockFetchOk("asset-OK");

    const { result } = renderHook(() => useAttachmentUploader());
    const file = makeImageFile("ok.png");

    let tempId = "";
    await act(async () => {
      tempId = result.current.upload(file, "thread-1");
    });

    // uploading 中（fetch 还没 resolve），但 act 已经触发同步 setState
    // 等 microtask flush 后 ready
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.attachments).toHaveLength(1);
    const a = result.current.attachments[0];
    expect(a.id).toBe(tempId);
    expect(a.status).toBe("ready");
    expect(a.attachment).toEqual(expected);
    expect(result.current.readyAttachments).toEqual([expected]);
    expect(result.current.hasUploading).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/uploads/images");
    expect((init as RequestInit).method).toBe("POST");
  });

  it("上传失败 415 mime_not_allowed → status=failed + error_code 正确", async () => {
    mockFetchFail(415, "mime_not_allowed", "x");
    const { result } = renderHook(() => useAttachmentUploader());
    await act(async () => {
      result.current.upload(makeImageFile("bad.png"), "thread-1");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(1);
    expect(result.current.attachments[0].status).toBe("failed");
    expect(result.current.attachments[0].error_code).toBe("mime_not_allowed");
    expect(result.current.readyAttachments).toEqual([]);
    expect(result.current.failedCount).toBe(1);
  });

  it("上传失败 413 too_large → error_code='too_large'", async () => {
    mockFetchFail(413, "too_large");
    const { result } = renderHook(() => useAttachmentUploader());
    await act(async () => {
      result.current.upload(makeImageFile("big.png"), "thread-1");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments[0].error_code).toBe("too_large");
  });

  it("客户端预校验拒绝 mime（image/bmp）→ 直接 failed，fetch 不被调用", async () => {
    const { result } = renderHook(() => useAttachmentUploader());
    const bmp = new File([new Uint8Array(1024)], "a.bmp", { type: "image/bmp" });
    await act(async () => {
      result.current.upload(bmp, "thread-1");
    });
    expect(result.current.attachments).toHaveLength(1);
    expect(result.current.attachments[0].status).toBe("failed");
    expect(result.current.attachments[0].error_code).toBe("mime_not_allowed");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("客户端预校验拒绝 size（>5MB）→ 直接 failed，fetch 不被调用", async () => {
    const { result } = renderHook(() => useAttachmentUploader());
    const huge = makeImageFile("huge.png", "image/png", 5 * 1024 * 1024 + 1);
    await act(async () => {
      result.current.upload(huge, "thread-1");
    });
    expect(result.current.attachments[0].status).toBe("failed");
    expect(result.current.attachments[0].error_code).toBe("too_large");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("remove(id) → 列表缩短", async () => {
    mockFetchOk("a1");
    mockFetchOk("a2");
    const { result } = renderHook(() => useAttachmentUploader());

    let id1 = "";
    let id2 = "";
    await act(async () => {
      id1 = result.current.upload(makeImageFile("1.png"), "tid");
      id2 = result.current.upload(makeImageFile("2.png"), "tid");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(2);

    await act(async () => {
      result.current.remove(id1);
    });
    expect(result.current.attachments).toHaveLength(1);
    expect(result.current.attachments[0].id).toBe(id2);
  });

  it("clear() → 清空所有", async () => {
    mockFetchOk("a1");
    mockFetchOk("a2");
    const { result } = renderHook(() => useAttachmentUploader());
    await act(async () => {
      result.current.upload(makeImageFile("1.png"), "tid");
      result.current.upload(makeImageFile("2.png"), "tid");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(2);
    await act(async () => {
      result.current.clear();
    });
    expect(result.current.attachments).toHaveLength(0);
    expect(result.current.readyAttachments).toHaveLength(0);
  });

  it("readyAttachments 只含 ready 状态的 attachment 字段", async () => {
    // 1 成功 + 1 客户端校验失败
    mockFetchOk("good");
    const { result } = renderHook(() => useAttachmentUploader());
    const bmp = new File([new Uint8Array(1024)], "bad.bmp", {
      type: "image/bmp",
    });
    await act(async () => {
      result.current.upload(makeImageFile("ok.png"), "tid");
      result.current.upload(bmp, "tid");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(2);
    expect(result.current.readyAttachments).toHaveLength(1);
    expect(result.current.readyAttachments[0].asset_id).toBe("good");
  });

  it("hasUploading：上传中 true，完成后 false", async () => {
    // 控制 fetch resolution 时机
    let resolveFn!: (v: unknown) => void;
    const pending = new Promise<unknown>((res) => {
      resolveFn = res;
    });
    fetchMock.mockReturnValueOnce(pending);

    const { result } = renderHook(() => useAttachmentUploader());

    await act(async () => {
      result.current.upload(makeImageFile("slow.png"), "tid");
    });
    expect(result.current.hasUploading).toBe(true);

    await act(async () => {
      resolveFn({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => makeReadyAttachment("slow"),
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.hasUploading).toBe(false);
  });

  it("数量校验（DoD #9）：已有 6 张 ready 时第 7 张被拒，attachments 仍 6 且 toast", async () => {
    const { toast } = await import("sonner");
    (toast.error as Mock).mockClear();

    // 让 fetch 顺序返回 6 个 ready
    for (let i = 0; i < 6; i++) {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => makeReadyAttachment(`asset-${i}`),
      });
    }

    const { result } = renderHook(() => useAttachmentUploader());

    await act(async () => {
      for (let i = 0; i < 6; i++) {
        result.current.upload(makeImageFile(`p${i}.png`), "tid");
      }
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(6);
    expect(result.current.readyAttachments).toHaveLength(6);
    (toast.error as Mock).mockClear();

    // 第 7 次 upload 被拒
    await act(async () => {
      result.current.upload(makeImageFile("seventh.png"), "tid");
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(6);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining("6"),
    );
    // 不发请求（fetch 调用次数与前 6 张一致）
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("数量校验：快速粘贴 7 张（同步触发 7 次 upload）→ 仅前 6 张进列表", async () => {
    const { toast } = await import("sonner");
    (toast.error as Mock).mockClear();

    // 让 fetch 7 次都返回 ready（实际只会被调 6 次）
    for (let i = 0; i < 7; i++) {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => makeReadyAttachment(`asset-${i}`),
      });
    }

    const { result } = renderHook(() => useAttachmentUploader());

    // 同步触发 7 次（模拟用户粘贴一次塞了 7 张图）
    await act(async () => {
      for (let i = 0; i < 7; i++) {
        result.current.upload(makeImageFile(`burst-${i}.png`), "tid");
      }
      await Promise.resolve();
      await Promise.resolve();
    });

    // 原子校验：React 18 同事件循环 setAttachments updater 拿到最新 prev，
    // 第 7 次拒绝，第 1-6 张正常入列
    expect(result.current.attachments).toHaveLength(6);
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining("6"),
    );
  });
});

// ---------------------------------------------------------------------------
// §3 ThumbnailStrip 组件
// ---------------------------------------------------------------------------

describe("ThumbnailStrip", () => {
  function makeAttachment(
    id: string,
    status: AttachmentState["status"],
    extra: Partial<AttachmentState> = {},
  ): AttachmentState {
    return {
      id,
      status,
      filename: `${id}.png`,
      size_bytes: 1024,
      mime_type: "image/png",
      local_preview_url: `blob:fake-${id}`,
      ...extra,
    };
  }

  it("0 attachments → 不渲染容器 testid", () => {
    const { container } = render(
      <ThumbnailStrip attachments={[]} onRemove={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();
  });

  it("3 attachments → 渲染 3 张缩略图 + 容器 testid 存在", () => {
    render(
      <ThumbnailStrip
        attachments={[
          makeAttachment("a", "ready"),
          makeAttachment("b", "ready"),
          makeAttachment("c", "ready"),
        ]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByTestId("attachment-thumbnail-strip")).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(3);
  });

  it("点击 ✕ 按钮 → onRemove(id) 被调用", async () => {
    const onRemove = vi.fn();
    render(
      <ThumbnailStrip
        attachments={[makeAttachment("xyz", "ready")]}
        onRemove={onRemove}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId("thumbnail-remove-xyz"));
    expect(onRemove).toHaveBeenCalledWith("xyz");
  });

  it("uploading 状态显示 spinner（.animate-spin）", () => {
    const { container } = render(
      <ThumbnailStrip
        attachments={[makeAttachment("u1", "uploading")]}
        onRemove={vi.fn()}
      />,
    );
    const spinner = container.querySelector(".animate-spin");
    expect(spinner).not.toBeNull();
  });

  it("failed 状态显示红色边框 + title 属性", () => {
    const { container } = render(
      <ThumbnailStrip
        attachments={[
          makeAttachment("f1", "failed", { error_message: "boom" }),
        ]}
        onRemove={vi.fn()}
      />,
    );
    // 找带红边框 class 的容器
    const failedCell = container.querySelector(".border-red-500");
    expect(failedCell).not.toBeNull();
    expect(failedCell?.getAttribute("title")).toBe("boom");
  });
});

// ---------------------------------------------------------------------------
// §4 Composer 集成
// ---------------------------------------------------------------------------

describe("Composer 图片集成", () => {
  /**
   * 在 Composer 内部的 textarea 上派发原生 paste 事件，
   * 用 Object.defineProperty 注入 clipboardData（jsdom 默认不带）。
   */
  function pasteImageOnTextarea(textarea: HTMLElement, files: File[]): void {
    const items = files.map(makeClipboardItem);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: { items: items as unknown as DataTransferItemList },
    });
    fireEvent(textarea, event);
  }

  it("粘贴 PNG → ThumbnailStrip 出现一张缩略图（mock fetch 成功）", async () => {
    mockFetchOk("ok-1");
    render(<Composer onSubmit={vi.fn()} threadId="t1" />);
    const ta = screen.getByLabelText("消息输入");

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(
      screen.getByTestId("attachment-thumbnail-strip"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("上传中点发送 → onSubmit 不被调用（hasUploading 阻止）", async () => {
    // fetch 挂起，不 resolve
    fetchMock.mockReturnValueOnce(new Promise(() => undefined));
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} threadId="t1" />);
    const ta = screen.getByLabelText("消息输入");

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
    });

    // 发送按钮 disabled
    const sendBtn = screen.getByTestId("composer-send");
    expect(sendBtn).toBeDisabled();
    // 强行 click 也应该不触发 onSubmit
    await userEvent.setup().click(sendBtn);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("上传完成点发送 → onSubmit 被调用，第 3 个参数是 ready attachments", async () => {
    const expected = mockFetchOk("uploaded-1");
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} threadId="t1" />);
    const ta = screen.getByLabelText("消息输入");
    const user = userEvent.setup();

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
      await Promise.resolve();
    });

    // 输点文本一起发
    await user.type(ta, "hi");
    await user.click(screen.getByTestId("composer-send"));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toBe("hi");
    expect(onSubmit.mock.calls[0][1]).toBe(null);
    expect(onSubmit.mock.calls[0][2]).toEqual([expected]);
  });

  it("restoreDraftToken 变化时恢复最近一次提交的 ready 图片附件", async () => {
    const expected = mockFetchOk("uploaded-restore");
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer onSubmit={onSubmit} threadId="t1" restoreDraftToken={null} />,
    );
    const ta = screen.getByLabelText("消息输入");
    const user = userEvent.setup();

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
      await Promise.resolve();
    });

    await user.click(screen.getByTestId("composer-send"));
    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();

    rerender(
      <Composer onSubmit={onSubmit} threadId="t1" restoreDraftToken={1} />,
    );

    expect(screen.getByTestId("attachment-thumbnail-strip")).toBeInTheDocument();
    await user.click(screen.getByTestId("composer-send"));

    expect(onSubmit).toHaveBeenLastCalledWith(
      "",
      null,
      [expected],
      undefined,
      {
        text: "",
        reasoningEffort: null,
        attachments: [expected],
        references: [],
      },
    );
  });

  it("发送后 → ThumbnailStrip 被清空（uploader.clear 触发）", async () => {
    mockFetchOk("uploaded-x");
    const onSubmit = vi.fn();
    render(<Composer onSubmit={onSubmit} threadId="t1" />);
    const ta = screen.getByLabelText("消息输入");
    const user = userEvent.setup();

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("attachment-thumbnail-strip")).toBeInTheDocument();

    await user.type(ta, "hi");
    await user.click(screen.getByTestId("composer-send"));

    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();
  });

  it("threadId 未提供 → 粘贴 PNG 被吞掉（uploader 不调 upload）", async () => {
    render(<Composer onSubmit={vi.fn()} />);
    const ta = screen.getByLabelText("消息输入");

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();
  });

  it("disabled=true → 粘贴 PNG 被吞掉", async () => {
    render(<Composer onSubmit={vi.fn()} threadId="t1" disabled={true} />);
    const ta = screen.getByLabelText("消息输入");

    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();
  });

  // ---- R2 boundary fix P0 #1: 切换 threadId → uploader / textarea 自动清空 ----
  it("切换 threadId → uploader 自动清空 + textarea 清空", async () => {
    mockFetchOk("asset-thread-a");
    const onSubmit = vi.fn();
    const { rerender } = render(
      <Composer onSubmit={onSubmit} threadId="thread-a" />,
    );
    const ta = screen.getByLabelText("消息输入") as HTMLTextAreaElement;

    // 在 thread-a 粘贴一张 + 输入草稿
    await act(async () => {
      pasteImageOnTextarea(ta, [makeImageFile("a.png")]);
      await Promise.resolve();
      await Promise.resolve();
    });
    await userEvent.setup().type(ta, "draft for thread A");
    expect(
      screen.getByTestId("attachment-thumbnail-strip"),
    ).toBeInTheDocument();
    expect(ta.value).toBe("draft for thread A");

    // 切换到 thread-b（rerender 改 threadId prop）
    await act(async () => {
      rerender(<Composer onSubmit={onSubmit} threadId="thread-b" />);
    });

    // 缩略图消失（uploader.clear 执行）
    expect(screen.queryByTestId("attachment-thumbnail-strip")).toBeNull();
    // textarea 清空（避免把 thread-a 草稿发到 thread-b）
    const ta2 = screen.getByLabelText("消息输入") as HTMLTextAreaElement;
    expect(ta2.value).toBe("");
  });
});

// ---------------------------------------------------------------------------
// §5 R2 boundary fix 补充用例
// ---------------------------------------------------------------------------

describe("R2 boundary fix: useAttachmentUploader 边界", () => {
  it("客户端 mime 失败后 countRef 回退 → 仍能添加 6 张合法图（P1 #3）", async () => {
    const { toast } = await import("sonner");
    (toast.error as Mock).mockClear();

    const { result } = renderHook(() => useAttachmentUploader());

    // 连粘 6 张 bmp（客户端 mime 校验拒绝；不调 fetch）
    await act(async () => {
      for (let i = 0; i < 6; i++) {
        const bmp = new File([new Uint8Array(1024)], `bad-${i}.bmp`, {
          type: "image/bmp",
        });
        result.current.upload(bmp, "tid");
      }
      await Promise.resolve();
    });
    expect(result.current.attachments).toHaveLength(6);
    expect(result.current.attachments.every((a) => a.status === "failed")).toBe(
      true,
    );
    expect(fetchMock).not.toHaveBeenCalled();

    // 再粘 6 张正常 png：countRef 已被回退到 0，应能全部入列且不触发 count_exceeded
    for (let i = 0; i < 6; i++) {
      mockFetchOk(`good-${i}`);
    }
    await act(async () => {
      for (let i = 0; i < 6; i++) {
        result.current.upload(makeImageFile(`good-${i}.png`), "tid");
      }
      await Promise.resolve();
      await Promise.resolve();
    });

    // 总条目数 = 6 failed + 6 ready
    expect(result.current.attachments).toHaveLength(12);
    expect(result.current.readyAttachments).toHaveLength(6);
    expect(fetchMock).toHaveBeenCalledTimes(6);
    // 没出现 count_exceeded toast
    const errorCalls = (toast.error as Mock).mock.calls;
    expect(
      errorCalls.some((call) =>
        typeof call[0] === "string" && call[0].includes("最多"),
      ),
    ).toBe(false);
  });

  it("0 字节文件 → 直接 failed empty_payload，fetch 不调用（P1 #4）", async () => {
    const { result } = renderHook(() => useAttachmentUploader());
    const empty = new File([new Uint8Array(0)], "empty.png", {
      type: "image/png",
    });
    expect(empty.size).toBe(0);

    await act(async () => {
      result.current.upload(empty, "tid");
      await Promise.resolve();
    });

    expect(result.current.attachments).toHaveLength(1);
    expect(result.current.attachments[0].status).toBe("failed");
    expect(result.current.attachments[0].error_code).toBe("empty_payload");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("0 字节文件 failed 后 countRef 回退 → 仍能正常上传 6 张", async () => {
    const { result } = renderHook(() => useAttachmentUploader());

    // 先粘 1 张 0 字节
    await act(async () => {
      const empty = new File([new Uint8Array(0)], "empty.png", {
        type: "image/png",
      });
      result.current.upload(empty, "tid");
      await Promise.resolve();
    });
    expect(result.current.attachments[0].error_code).toBe("empty_payload");

    // 再粘 6 张正常的：不应被 count_exceeded 拒
    for (let i = 0; i < 6; i++) {
      mockFetchOk(`ok-${i}`);
    }
    await act(async () => {
      for (let i = 0; i < 6; i++) {
        result.current.upload(makeImageFile(`ok-${i}.png`), "tid");
      }
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.readyAttachments).toHaveLength(6);
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });
});
