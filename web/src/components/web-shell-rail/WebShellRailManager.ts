import type {
  WebShellRailContext,
  WebShellRailItem,
  WebShellRailLayoutConfig,
  WebShellRailOpenSource,
  WebShellRailPriority,
  WebShellRailState,
} from "./types";

interface WebShellRailManagerOptions {
  context: WebShellRailContext;
  items: WebShellRailItem[];
  layoutConfig?: Partial<WebShellRailLayoutConfig>;
}

const PRIORITY_ORDER: Record<WebShellRailPriority, number> = {
  p0: 0,
  p1: 1,
  p2: 2,
};

const DEFAULT_LAYOUT_CONFIG: WebShellRailLayoutConfig = {
  railWidthPx: 52,
  hoverZoneWidthPx: 18,
  hoverZoneHeightPx: 420,
  buttonSizePx: 36,
  buttonGapPx: 8,
  zIndex: 45,
  overlayLeftSidebar: true,
};

const PANEL_VERTICAL_PADDING_PX = 16;

function assertUniqueItemIds(items: WebShellRailItem[]): void {
  const seen = new Set<string>();
  const duplicated = new Set<string>();
  for (const item of items) {
    if (seen.has(item.id)) {
      duplicated.add(item.id);
    }
    seen.add(item.id);
  }
  if (duplicated.size > 0) {
    throw new Error(
      `Duplicate WebShellRail item id: ${[...duplicated].sort().join(", ")}`,
    );
  }
}

function getVisibleLimit(config: WebShellRailLayoutConfig): number {
  const availableHeight = Math.max(
    config.buttonSizePx,
    config.hoverZoneHeightPx - PANEL_VERTICAL_PADDING_PX,
  );
  return Math.max(
    1,
    Math.floor(
      (availableHeight + config.buttonGapPx) /
        (config.buttonSizePx + config.buttonGapPx),
    ),
  );
}

function hasRequiredCapability(
  context: WebShellRailContext,
  item: WebShellRailItem,
): boolean {
  if (!item.requiredCapability) return true;
  return context.capabilities[item.requiredCapability] === true;
}

export class WebShellRailManager {
  private readonly context: WebShellRailContext;
  private readonly items: WebShellRailItem[];
  private readonly layoutConfig: WebShellRailLayoutConfig;

  constructor(options: WebShellRailManagerOptions) {
    this.context = options.context;
    assertUniqueItemIds(options.items);
    this.items = options.items;
    this.layoutConfig = {
      ...DEFAULT_LAYOUT_CONFIG,
      ...options.layoutConfig,
      overlayLeftSidebar: true,
    };
  }

  getLayoutConfig(): WebShellRailLayoutConfig {
    if (this.context.density === "compact") {
      return {
        ...this.layoutConfig,
        hoverZoneHeightPx: Math.min(this.layoutConfig.hoverZoneHeightPx, 340),
      };
    }
    return this.layoutConfig;
  }

  createState(
    open: boolean,
    openedBy: WebShellRailOpenSource,
  ): WebShellRailState {
    return {
      open,
      openedBy,
      density: this.context.density,
      visibleItems: this.resolveVisibleItems(),
    };
  }

  resolveVisibleItems(): WebShellRailItem[] {
    if (this.context.density === "mobile") return [];
    const allowedPriorities =
      this.context.density === "compact"
        ? new Set<WebShellRailPriority>(["p0", "p1"])
        : new Set<WebShellRailPriority>(["p0", "p1", "p2"]);
    const visibleItems = this.items
      .filter((item) => item.available)
      .filter((item) => hasRequiredCapability(this.context, item))
      .filter((item) => allowedPriorities.has(item.priority))
      .sort((a, b) => {
        const priorityDiff = PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
        if (priorityDiff !== 0) return priorityDiff;
        if (a.scope !== b.scope) return a.scope === "global" ? -1 : 1;
        return a.id.localeCompare(b.id);
      });
    if (this.context.density !== "compact") return visibleItems;
    return visibleItems.slice(0, getVisibleLimit(this.getLayoutConfig()));
  }
}
