import type { ComponentProps } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  WebShellRail,
  WebShellRailManager,
  WebShellRailProvider,
  useRegisterWebShellRailItems,
  useWebShellRailRegisteredItems,
} from "@/components/web-shell-rail";
import type {
  WebShellRailContext,
  WebShellRailItem,
} from "@/components/web-shell-rail";

function TestIcon({ className }: ComponentProps<"span">) {
  return <span className={className} />;
}

const context: WebShellRailContext = {
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

afterEach(() => {
  vi.restoreAllMocks();
});

function renderRail(items: WebShellRailItem[]) {
  const manager = new WebShellRailManager({ context, items });
  return render(
    <MemoryRouter>
      <WebShellRail manager={manager} />
    </MemoryRouter>,
  );
}

function makeRegistryItem(id: string): WebShellRailItem {
  return {
    id,
    scope: "thread",
    priority: "p0",
    label: id,
    icon: TestIcon,
    available: true,
  };
}

const registryFirstItems = [makeRegistryItem("first")];
const registrySecondItems = [makeRegistryItem("second")];

function RegisterItems({
  sourceId,
  items,
}: {
  sourceId: string;
  items: WebShellRailItem[];
}) {
  useRegisterWebShellRailItems(sourceId, items);
  return null;
}

function RegisteredItemsProbe() {
  const items = useWebShellRailRegisteredItems();
  return <div data-testid="registered-items">{items.map((item) => item.id).join(",")}</div>;
}

function RegistryHarness({
  showFirst,
  showSecond,
}: {
  showFirst: boolean;
  showSecond: boolean;
}) {
  return (
    <WebShellRailProvider>
      {showFirst ? (
        <RegisterItems
          key="first"
          sourceId="shared-source"
          items={registryFirstItems}
        />
      ) : null}
      {showSecond ? (
        <RegisterItems
          key="second"
          sourceId="shared-source"
          items={registrySecondItems}
        />
      ) : null}
      <RegisteredItemsProbe />
    </WebShellRailProvider>
  );
}

describe("WebShellRail", () => {
  it("uses a tall hover zone and opens or closes from hover and Escape", () => {
    renderRail([
      {
        id: "manage",
        scope: "global",
        priority: "p0",
        label: "管理",
        icon: TestIcon,
        available: true,
        to: "/manage",
      },
    ]);

    const rail = screen.getByTestId("web-shell-rail");
    expect(rail).toHaveAttribute("data-open", "false");
    expect(rail).toHaveAttribute("data-density", "desktop");
    expect(rail).toHaveStyle({
      zIndex: "45",
      width: "18px",
      height: "420px",
    });
    expect(screen.getByTestId("web-shell-rail-hover-zone")).toBeInTheDocument();

    fireEvent.mouseEnter(rail);

    expect(rail).toHaveAttribute("data-open", "true");
    expect(rail).toHaveStyle({ width: "70px" });
    expect(screen.getByTestId("web-shell-rail-panel")).toHaveStyle({
      gap: "8px",
      maxHeight: "404px",
    });
    expect(screen.getByTestId("web-shell-rail-panel")).toHaveClass(
      "overflow-y-auto",
    );

    fireEvent.keyDown(rail, { key: "Escape" });

    expect(rail).toHaveAttribute("data-open", "false");
  });

  it("opens on focus, closes on blur, and closes after an action item runs", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    renderRail([
      {
        id: "logs",
        scope: "global",
        priority: "p1",
        label: "日志",
        icon: TestIcon,
        available: true,
        onSelect,
      },
    ]);

    const rail = screen.getByTestId("web-shell-rail");
    const button = screen.getByTestId("web-shell-rail-item-logs");
    fireEvent.focus(button);

    expect(rail).toHaveAttribute("data-open", "true");

    fireEvent.blur(button, { relatedTarget: document.body });

    expect(rail).toHaveAttribute("data-open", "false");

    fireEvent.mouseEnter(rail);
    await user.click(button);

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(rail).toHaveAttribute("data-open", "false");
  });

  it("closes and reports action failures with item context", async () => {
    const user = userEvent.setup();
    const error = new Error("boom");
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderRail([
      {
        id: "logs",
        scope: "global",
        priority: "p1",
        label: "日志",
        icon: TestIcon,
        available: true,
        onSelect: () => Promise.reject(error),
      },
    ]);

    const rail = screen.getByTestId("web-shell-rail");
    const button = screen.getByTestId("web-shell-rail-item-logs");
    fireEvent.mouseEnter(rail);

    await user.click(button);

    expect(rail).toHaveAttribute("data-open", "false");
    expect(consoleError).toHaveBeenCalledWith(
      "WebShellRail item failed: logs (日志)",
      error,
    );
  });

  it("keeps the latest registration when an older duplicate source unmounts", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const { rerender } = render(
      <RegistryHarness showFirst={true} showSecond={true} />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("registered-items")).toHaveTextContent("second"),
    );

    rerender(<RegistryHarness showFirst={false} showSecond={true} />);

    await waitFor(() =>
      expect(screen.getByTestId("registered-items")).toHaveTextContent("second"),
    );
  });
});
