import type { ComponentProps } from "react";
import { describe, expect, it } from "vitest";
import { WebShellRailManager } from "@/components/web-shell-rail";
import type {
  WebShellRailContext,
  WebShellRailItem,
} from "@/components/web-shell-rail";

function TestIcon({ className }: ComponentProps<"span">) {
  return <span className={className} />;
}

const desktopContext: WebShellRailContext = {
  density: "desktop",
  activeThreadId: "thread-1",
  activeThreadTitle: "Thread",
  hasActiveThread: true,
  isAuthenticated: true,
  hostEnvironment: "browser",
  capabilities: {
    xspaceHost: false,
    nativeFileDialog: false,
  },
};

function makeItem(overrides: Partial<WebShellRailItem>): WebShellRailItem {
  return {
    id: "item",
    scope: "global",
    priority: "p1",
    label: "item",
    icon: TestIcon,
    available: true,
    ...overrides,
  };
}

describe("WebShellRailManager", () => {
  it("orders available desktop items by priority, scope, and id", () => {
    const manager = new WebShellRailManager({
      context: desktopContext,
      items: [
        makeItem({ id: "thread-z", scope: "thread", priority: "p1" }),
        makeItem({ id: "global-b", scope: "global", priority: "p1" }),
        makeItem({ id: "hidden", priority: "p0", available: false }),
        makeItem({ id: "thread-a", scope: "thread", priority: "p0" }),
        makeItem({ id: "global-a", scope: "global", priority: "p0" }),
        makeItem({ id: "global-p2", scope: "global", priority: "p2" }),
      ],
    });

    expect(manager.resolveVisibleItems().map((item) => item.id)).toEqual([
      "global-a",
      "thread-a",
      "global-b",
      "thread-z",
      "global-p2",
    ]);
  });

  it("keeps compact mode to p0 and p1 items and shortens the hover zone", () => {
    const manager = new WebShellRailManager({
      context: { ...desktopContext, density: "compact" },
      items: [
        makeItem({ id: "p0", priority: "p0" }),
        makeItem({ id: "p1", priority: "p1" }),
        makeItem({ id: "p2", priority: "p2" }),
      ],
      layoutConfig: { hoverZoneHeightPx: 420 },
    });

    expect(manager.resolveVisibleItems().map((item) => item.id)).toEqual([
      "p0",
      "p1",
    ]);
    expect(manager.getLayoutConfig().hoverZoneHeightPx).toBe(340);
  });

  it("limits compact visible items to the hover zone capacity", () => {
    const manager = new WebShellRailManager({
      context: { ...desktopContext, density: "compact" },
      items: Array.from({ length: 9 }, (_, index) =>
        makeItem({ id: `p1-${index}`, priority: "p1" }),
      ),
      layoutConfig: { hoverZoneHeightPx: 340 },
    });

    expect(manager.resolveVisibleItems().map((item) => item.id)).toEqual([
      "p1-0",
      "p1-1",
      "p1-2",
      "p1-3",
      "p1-4",
      "p1-5",
      "p1-6",
    ]);
  });

  it("filters items by required capability before density rules", () => {
    const manager = new WebShellRailManager({
      context: {
        ...desktopContext,
        density: "compact",
        hostEnvironment: "xspace",
        capabilities: {
          xspaceHost: true,
          nativeFileDialog: false,
        },
      },
      items: [
        makeItem({ id: "general", priority: "p0" }),
        makeItem({
          id: "xspace-only",
          priority: "p1",
          requiredCapability: "xspaceHost",
        }),
        makeItem({
          id: "native-only",
          priority: "p1",
          requiredCapability: "nativeFileDialog",
        }),
        makeItem({ id: "p2", priority: "p2" }),
      ],
    });

    expect(manager.resolveVisibleItems().map((item) => item.id)).toEqual([
      "general",
      "xspace-only",
    ]);
  });

  it("rejects duplicate item ids before rendering ambiguous keys", () => {
    expect(
      () =>
        new WebShellRailManager({
          context: desktopContext,
          items: [
            makeItem({ id: "duplicate" }),
            makeItem({ id: "duplicate", scope: "thread" }),
          ],
        }),
    ).toThrow("Duplicate WebShellRail item id: duplicate");
  });

  it("removes rail items on mobile and keeps z-index above LeftSidebar", () => {
    const manager = new WebShellRailManager({
      context: { ...desktopContext, density: "mobile" },
      items: [makeItem({ id: "p0", priority: "p0" })],
    });

    expect(manager.createState(true, "hover")).toMatchObject({
      density: "mobile",
      open: true,
      openedBy: "hover",
      visibleItems: [],
    });
    expect(manager.getLayoutConfig()).toMatchObject({
      overlayLeftSidebar: true,
      zIndex: 45,
    });
  });
});
