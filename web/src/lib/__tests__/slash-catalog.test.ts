import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fetchSlashCatalogGroupItems,
  fetchSlashCatalogGroups,
  fetchSlashCatalogItems,
} from "@/lib/slash-catalog";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = vi.fn() as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.resetAllMocks();
});

describe("slash catalog api client", () => {
  it("fetches groups with encoded thread_id", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ groups: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await fetchSlashCatalogGroups("thread-a/b");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/slash-catalog?thread_id=thread-a%2Fb",
    );
  });

  it("fetches group items with encoded group id and thread_id", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ group: { id: "skill" }, items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await fetchSlashCatalogGroupItems("skill/a", "thread-a/b");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/slash-catalog/groups/skill%2Fa?thread_id=thread-a%2Fb",
    );
  });

  it("loads every group and flattens leaf items in group order", async () => {
    const commandItem = { id: "command:/evolve" };
    const skillItem = { id: "skill:review" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            groups: [
              { id: "command" },
              { id: "skill" },
            ],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            group: { id: "command" },
            items: [commandItem],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            group: { id: "skill" },
            items: [skillItem],
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const items = await fetchSlashCatalogItems("thread-a/b");

    expect(items).toEqual([commandItem, skillItem]);
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/api/slash-catalog?thread_id=thread-a%2Fb",
      "/api/slash-catalog/groups/command?thread_id=thread-a%2Fb",
      "/api/slash-catalog/groups/skill?thread_id=thread-a%2Fb",
    ]);
  });
});
