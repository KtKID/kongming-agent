import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  ApiError,
  RateLimitedError,
  apiGet,
  apiGetThreadTaskProgress,
  apiPost,
} from "@/lib/api";

const originalFetch = globalThis.fetch;

describe("lib/api", () => {
  beforeEach(() => {
    // 替换 fetch
    globalThis.fetch = vi.fn() as unknown as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("happy path: 200 → JSON 解析", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const r = await apiGet<{ ok: boolean }>("/api/test");
    expect(r).toEqual({ ok: true });
  });

  it("X-Requested-With header 自动加上", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    await apiPost("/api/x", { a: 1 });
    const call = fetchMock.mock.calls[0];
    const init = call[1] as RequestInit;
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Requested-With"]).toBe("XMLHttpRequest");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("POST");
  });

  it("apiGetThreadTaskProgress encodes thread id in endpoint path", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            schema_version: 1,
            session_id: "thread-a/b",
            updated_at_ms: 1,
            source: "api",
            tasks: [],
            counts: {
              pending: 0,
              in_progress: 0,
              completed: 0,
              total: 0,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await apiGetThreadTaskProgress("thread-a/b");

    const call = fetchMock.mock.calls[0];
    expect(call[0]).toBe("/api/threads/thread-a%2Fb/task-progress");
  });

  it("401 → 抛 ApiError + redirect /login", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(null, { status: 401 }),
    );
    // location.replace mock
    const replaceSpy = vi.fn();
    Object.defineProperty(window, "location", {
      writable: true,
      value: { ...window.location, pathname: "/chat", replace: replaceSpy },
    });
    await expect(apiGet("/api/test")).rejects.toThrow(ApiError);
    expect(replaceSpy).toHaveBeenCalledWith("/login");
  });

  it("429 → RateLimitedError(Retry-After)", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(null, {
        status: 429,
        headers: { "Retry-After": "60" },
      }),
    );
    await expect(apiGet("/api/test")).rejects.toMatchObject({
      retryAfterSeconds: 60,
    });
    await expect(apiGet("/api/test")).rejects.toBeInstanceOf(
      RateLimitedError,
    );
  });

  it("500 + ErrorResponseDTO → 抛 ApiError 含 errorCode/details", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "internal",
          message: "boom",
          details: { foo: 1 },
        }),
        {
          status: 500,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    try {
      await apiGet("/api/test");
      expect.fail("should throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      const e = err as ApiError;
      expect(e.status).toBe(500);
      expect(e.errorCode).toBe("internal");
      expect(e.detail).toBe("boom");
      expect(e.details).toEqual({ foo: 1 });
    }
  });

  it("网络异常 → ApiError(0, network)", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    try {
      await apiGet("/api/test");
      expect.fail("should throw");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(0);
      expect((err as ApiError).errorCode).toBe("network");
    }
  });

  it("204 No Content → undefined", async () => {
    (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const r = await apiGet<undefined>("/api/test");
    expect(r).toBeUndefined();
  });
});
