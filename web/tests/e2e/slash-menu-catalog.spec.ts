import { expect, test, type Page, type Route } from "@playwright/test";

const VITE_DEV_URL = "http://127.0.0.1:5174";
const THREAD_ID = "thread-slash000001";

async function installFakeWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FakeWebSocket {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;

      readonly url: string;
      readyState = FakeWebSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;

      constructor(url: string) {
        this.url = url;
        window.setTimeout(() => {
          this.readyState = FakeWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        }, 0);
      }

      send(data: string): void {
        try {
          const frame = JSON.parse(data) as { frame_type?: string; ts?: number };
          if (frame.frame_type === "ping") {
            this.onmessage?.(
              new MessageEvent("message", {
                data: JSON.stringify({ frame_type: "pong", ts: frame.ts }),
              }),
            );
          }
        } catch {
          // Ignore non-JSON test frames.
        }
      }

      close(code = 1000, reason = "test-close"): void {
        this.readyState = FakeWebSocket.CLOSED;
        this.onclose?.(new CloseEvent("close", { code, reason }));
      }
    }

    Object.assign(FakeWebSocket, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
    window.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  });
}

async function stubBackend(page: Page): Promise<string[]> {
  const catalogRequests: string[] = [];
  const thread = {
    id: THREAD_ID,
    name: "Slash e2e",
    preset_id: "preset-a",
    backend_kind: "generic_chat",
    claude_thread_id: "",
    codex_thread_id: "",
    cwd: "E:/xgt/proj/agent-proj/kongming-agent",
    created_at: 1,
    updated_at: 2,
    message_count: 0,
    is_pinned: false,
    is_archived: false,
    thread_kind: "chat",
  };

  await page.route("**/api/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "[]",
    }),
  );
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    }),
  );
  await page.route("**/api/config/client", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        host_environment: "browser",
        capabilities: { xspace_host: false, native_file_dialog: false },
        ws_heartbeat_interval_ms: 30000,
        ws_heartbeat_background_interval_ms: 60000,
        ws_heartbeat_timeout_ms: 10000,
        ws_heartbeat_max_missed: 3,
        dashboard_poll_interval_seconds: 5,
        timezone: "Asia/Shanghai",
      }),
    }),
  );
  await page.route("**/api/threads", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([thread]),
    }),
  );
  await page.route("**/api/presets", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "preset-a",
          display_name: "Preset A",
          model: "fake-model",
          base_url_summary: "local",
          requires_api_key: false,
        },
      ]),
    }),
  );
  await page.route("**/api/model-providers/model-families", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/task-progress`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: 2,
        session_id: THREAD_ID,
        workflow_id: null,
        title: null,
        control_mode: null,
        updated_at_ms: 0,
        tasks: [],
        counts: {
          pending: 0,
          in_progress: 0,
          completed: 0,
          failed: 0,
          cancelled: 0,
          total: 0,
        },
      }),
    }),
  );
  await page.route(`**/api/threads/${THREAD_ID}/workspace-context`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        thread_id: THREAD_ID,
        workspace_root: "E:/xgt/proj/agent-proj/kongming-agent",
        backend_kind: "generic_chat",
      }),
    }),
  );
  const fulfillCatalogGroups = (route: Route) => {
    catalogRequests.push(route.request().url());
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        groups: [
          {
            id: "workflow",
            title: "Workflow",
            description: "Registered workflows",
            order: 10,
            item_count: 1,
            diagnostics: [],
          },
          {
            id: "command",
            title: "Command",
            description: "Registered commands",
            order: 20,
            item_count: 1,
            diagnostics: [],
          },
          {
            id: "skill",
            title: "Skill",
            description: "Registered skills",
            order: 30,
            item_count: 1,
            diagnostics: [],
          },
        ],
      }),
    });
  };
  await page.route("**/api/slash-catalog", fulfillCatalogGroups);
  await page.route("**/api/slash-catalog?**", fulfillCatalogGroups);
  await page.route("**/api/slash-catalog/groups/workflow*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        group: {
          id: "workflow",
          title: "Workflow",
          description: "Registered workflows",
          order: 10,
          item_count: 1,
          diagnostics: [],
        },
        items: [
          {
            id: "workflow:fake",
            group_id: "workflow",
            kind: "workflow_strategy",
            title: "Fake Workflow",
            description: "Fake workflow summary",
            source_ref: "workflow_strategy:fake",
            order: 0,
            section_id: "registered",
            slash: null,
            insert_text: "/workflow fake ",
            action: "insert_text",
            enabled: true,
            metadata: { mode: "fake" },
            diagnostics: [],
          },
        ],
      }),
    }),
  );
  return catalogRequests;
}

test.describe("slash menu catalog smoke", () => {
  test("thread composer opens catalog groups and inserts a workflow item", async ({
    page,
  }) => {
    await installFakeWebSocket(page);
    const catalogRequests = await stubBackend(page);

    await page.goto(`${VITE_DEV_URL}/chat/${THREAD_ID}`);

    const input = page.locator("textarea").first();
    await expect(input).toBeEnabled({ timeout: 10_000 });

    await input.fill("/");
    const menu = page.getByTestId("slash-menu");
    await expect(menu.getByText("Workflow")).toBeVisible();
    await expect(menu.getByText("Command")).toBeVisible();
    await expect(menu.getByText("Skill")).toBeVisible();

    await menu.getByRole("button", { name: /Workflow/ }).click();
    const workflowItem = menu.getByRole("button", { name: /Fake Workflow/ });
    await expect(workflowItem).toBeVisible();
    await workflowItem.click();

    await expect(input).toHaveValue("/workflow fake ");
    expect(
      catalogRequests.some((url) =>
        url.includes(`/api/slash-catalog?thread_id=${THREAD_ID}`),
      ),
    ).toBe(true);
  });
});
