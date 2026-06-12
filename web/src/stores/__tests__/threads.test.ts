import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LLMPresetDTO, ThreadMetadataDTO } from "@/protocol";

const apiMocks = vi.hoisted(() => ({
  apiDelete: vi.fn(),
  apiGet: vi.fn(),
  apiPatch: vi.fn(),
  apiPost: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiDelete: apiMocks.apiDelete,
  apiGet: apiMocks.apiGet,
  apiPatch: apiMocks.apiPatch,
  apiPost: apiMocks.apiPost,
}));

const THREADS_CACHE_KEY = "kongming.sidebar.threads";

function makeThread(overrides: Partial<ThreadMetadataDTO> = {}): ThreadMetadataDTO {
  return {
    id: "thread-111111111111",
    name: "thread",
    preset_id: "preset-a",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "",
    created_at: 100,
    updated_at: 100,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    schema_version: 10,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function loadStore() {
  const mod = await import("@/stores/threads");
  return mod.useThreadsStore;
}

describe("stores/threads", () => {
  beforeEach(() => {
    vi.resetModules();
    apiMocks.apiDelete.mockReset();
    apiMocks.apiGet.mockReset();
    apiMocks.apiPatch.mockReset();
    apiMocks.apiPost.mockReset();
    localStorage.clear();
  });

  it("cold start loads cached threads and sorts pinned first", async () => {
    localStorage.setItem(
      THREADS_CACHE_KEY,
      JSON.stringify([
        makeThread({ id: "thread-aaaaaaaaaaaa", updated_at: 10, is_pinned: false }),
        makeThread({ id: "thread-bbbbbbbbbbbb", updated_at: 5, is_pinned: true }),
        makeThread({ id: "thread-cccccccccccc", updated_at: 20, is_pinned: false }),
      ]),
    );

    const useThreadsStore = await loadStore();

    expect(useThreadsStore.getState().threads.map((thread) => thread.id)).toEqual([
      "thread-bbbbbbbbbbbb",
      "thread-cccccccccccc",
      "thread-aaaaaaaaaaaa",
    ]);
  });

  it("fetchThreads dedupes concurrent requests and writes sorted cache", async () => {
    const response = deferred<ThreadMetadataDTO[]>();
    apiMocks.apiGet.mockReturnValue(response.promise);
    const useThreadsStore = await loadStore();

    const p1 = useThreadsStore.getState().fetchThreads();
    const p2 = useThreadsStore.getState().fetchThreads();

    expect(apiMocks.apiGet).toHaveBeenCalledTimes(1);
    expect(useThreadsStore.getState().loading).toBe(true);

    response.resolve([
      makeThread({ id: "thread-aaaaaaaaaaaa", updated_at: 10, is_pinned: false }),
      makeThread({ id: "thread-bbbbbbbbbbbb", updated_at: 5, is_pinned: true }),
    ]);
    await Promise.all([p1, p2]);

    expect(useThreadsStore.getState().loading).toBe(false);
    expect(useThreadsStore.getState().threads.map((thread) => thread.id)).toEqual([
      "thread-bbbbbbbbbbbb",
      "thread-aaaaaaaaaaaa",
    ]);
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")).toHaveLength(2);
  });

  it("fetchPresets dedupes concurrent requests", async () => {
    const response = deferred<LLMPresetDTO[]>();
    apiMocks.apiGet.mockReturnValue(response.promise);
    const useThreadsStore = await loadStore();

    const p1 = useThreadsStore.getState().fetchPresets();
    const p2 = useThreadsStore.getState().fetchPresets();

    expect(apiMocks.apiGet).toHaveBeenCalledTimes(1);

    response.resolve([
      {
        id: "preset-a",
        display_name: "Preset A",
        model: "gpt-test",
        base_url_summary: "http://localhost",
        requires_api_key: false,
      },
    ]);
    await Promise.all([p1, p2]);

    expect(useThreadsStore.getState().presets).toHaveLength(1);
  });

  it("create, rename, pin, delete keep cached threads in sync", async () => {
    const useThreadsStore = await loadStore();
    const created = makeThread({
      id: "thread-created000",
      updated_at: 50,
      name: "created",
    });
    const renamed = {
      ...created,
      name: "renamed",
      updated_at: 60,
    };
    const pinned = {
      ...renamed,
      is_pinned: true,
      updated_at: 70,
    };

    apiMocks.apiPost.mockResolvedValue(created);
    await useThreadsStore.getState().createThread("created", "preset-a");
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")[0].id).toBe(
      "thread-created000",
    );

    apiMocks.apiPatch.mockResolvedValueOnce(renamed);
    await useThreadsStore.getState().renameThread(created.id, "renamed");
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")[0].name).toBe(
      "renamed",
    );

    apiMocks.apiPatch.mockResolvedValueOnce(pinned);
    await useThreadsStore.getState().pinThread(created.id, true);
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")[0].is_pinned).toBe(
      true,
    );

    apiMocks.apiDelete.mockResolvedValue(undefined);
    await useThreadsStore.getState().deleteThread(created.id);
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")).toEqual([]);
  });

  it("startPendingGenericThread enters generic pending state and clears initial message", async () => {
    const useThreadsStore = await loadStore();
    useThreadsStore.setState({ initialMessage: "stale" });

    useThreadsStore.getState().startPendingGenericThread();

    expect(useThreadsStore.getState().pendingNewSession).toEqual({
      backendKind: "generic_chat",
      cwd: "",
      projectName: "",
    });
    expect(useThreadsStore.getState().initialMessage).toBeNull();
  });

  it("createGenericThreadFromFirstMessage posts first-message endpoint and refreshes threads", async () => {
    const useThreadsStore = await loadStore();
    const created = makeThread({
      id: "thread-aaaaaaaaaaaa",
      name: "hello",
      cwd: "/tmp/project-a",
      updated_at: 200,
      message_count: 1,
    });
    apiMocks.apiPost.mockResolvedValueOnce({ thread: created });
    apiMocks.apiGet.mockResolvedValueOnce([created]);

    const result = await useThreadsStore.getState().createGenericThreadFromFirstMessage({
      text: "hello",
      preset_id: "preset-a",
      cwd: "/tmp/project-a",
      reasoning_effort: "high",
    });

    expect(apiMocks.apiPost).toHaveBeenCalledWith(
      "/api/threads/generic/first-message",
      {
        text: "hello",
        preset_id: "preset-a",
        cwd: "/tmp/project-a",
        reasoning_effort: "high",
      },
    );
    expect(apiMocks.apiGet).toHaveBeenCalledWith("/api/threads");
    expect(result).toEqual(created);
    expect(useThreadsStore.getState().threads).toEqual([created]);
    expect(useThreadsStore.getState().pendingNewSession).toBeNull();
  });

  it("updateThreadPreset patches preset endpoint and updates cached thread", async () => {
    const useThreadsStore = await loadStore();
    const original = makeThread({
      id: "thread-aaaaaaaaaaaa",
      preset_id: "preset-a",
      updated_at: 10,
    });
    const updated = {
      ...original,
      preset_id: "preset-b",
      updated_at: 20,
    };
    useThreadsStore.setState({ threads: [original] });
    localStorage.setItem(THREADS_CACHE_KEY, JSON.stringify([original]));
    apiMocks.apiPatch.mockResolvedValueOnce(updated);

    const result = await useThreadsStore
      .getState()
      .updateThreadPreset(original.id, "preset-b");

    expect(apiMocks.apiPatch).toHaveBeenCalledWith(
      "/api/threads/thread-aaaaaaaaaaaa/preset",
      { preset_id: "preset-b" },
    );
    expect(result.preset_id).toBe("preset-b");
    expect(useThreadsStore.getState().threads[0].preset_id).toBe("preset-b");
    expect(JSON.parse(localStorage.getItem(THREADS_CACHE_KEY) ?? "[]")[0].preset_id).toBe(
      "preset-b",
    );
  });
});
