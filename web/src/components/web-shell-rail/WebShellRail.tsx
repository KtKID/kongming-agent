import { useMemo, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { WebShellRailManager } from "./WebShellRailManager";
import type {
  WebShellRailItem,
  WebShellRailOpenSource,
  WebShellRailRenderProps,
} from "./types";

interface WebShellRailProps {
  manager: WebShellRailManager;
  className?: string;
}

export function WebShellRail({ manager, className }: WebShellRailProps) {
  const [open, setOpen] = useState(false);
  const [openedBy, setOpenedBy] = useState<WebShellRailOpenSource>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const state = manager.createState(open, openedBy);
  const layout = manager.getLayoutConfig();
  const itemButtonClassName = useMemo(
    () =>
      cn(
        "h-9 w-9 rounded-lg border border-border/80 bg-card/90 p-0 text-muted-foreground shadow-glass backdrop-blur-xl",
        "hover:border-primary/35 hover:bg-card hover:text-foreground",
        "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
      ),
    [],
  );
  const itemIconClassName = "h-4 w-4";

  if (state.visibleItems.length === 0) return null;

  const openBy = (source: Exclude<WebShellRailOpenSource, null>) => {
    setOpenedBy(source);
    setOpen(true);
  };

  const closeRail = () => {
    setOpen(false);
    setOpenedBy(null);
  };

  const handleFocusOut = (event: FocusEvent<HTMLDivElement>) => {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && rootRef.current?.contains(nextTarget)) {
      return;
    }
    closeRail();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    closeRail();
  };

  return (
    <div
        ref={rootRef}
        data-testid="web-shell-rail"
        data-open={state.open ? "true" : "false"}
        data-density={state.density}
        className={cn(
          "fixed left-0 top-1/2 isolate -translate-y-1/2",
          "flex items-center outline-none",
          className,
        )}
        style={{
          zIndex: layout.zIndex,
          width: state.open
            ? `${layout.railWidthPx + layout.hoverZoneWidthPx}px`
            : `${layout.hoverZoneWidthPx}px`,
          height: `${layout.hoverZoneHeightPx}px`,
        }}
        onMouseEnter={() => openBy("hover")}
        onMouseLeave={closeRail}
        onFocus={() => openBy("focus")}
        onBlur={handleFocusOut}
        onKeyDown={handleKeyDown}
      >
        <div
          aria-hidden="true"
          data-testid="web-shell-rail-hover-zone"
          className="absolute inset-y-0 left-0 w-full"
        />
        <div
          data-testid="web-shell-rail-panel"
          className={cn(
            "pointer-events-auto absolute left-2 top-1/2 flex -translate-y-1/2 flex-col items-center rounded-xl",
            "obsidian-panel obsidian-hairline overflow-y-auto border-border/80 bg-card/82 px-1.5 py-2 shadow-xl backdrop-blur-xl",
            "transition-[opacity,transform] duration-200 ease-out",
            state.open
              ? "translate-x-0 opacity-100"
              : "pointer-events-none -translate-x-3 opacity-0",
          )}
          style={{
            gap: `${layout.buttonGapPx}px`,
            maxHeight: `${Math.max(layout.buttonSizePx, layout.hoverZoneHeightPx - 16)}px`,
          }}
        >
          {state.visibleItems.map((item) => (
            <RailItemButton
              key={item.id}
              item={item}
              buttonClassName={itemButtonClassName}
              iconClassName={itemIconClassName}
              closeRail={closeRail}
            />
          ))}
        </div>
        <div
          aria-hidden="true"
          className={cn(
            "absolute left-0 top-1/2 h-14 w-1 -translate-y-1/2 rounded-r-full bg-primary/45 transition-opacity",
            state.open ? "opacity-0" : "opacity-100",
          )}
        />
    </div>
  );
}

function RailItemButton({
  item,
  buttonClassName,
  iconClassName,
  closeRail,
}: {
  item: WebShellRailItem;
  buttonClassName: string;
  iconClassName: string;
  closeRail: () => void;
}) {
  const renderProps: WebShellRailRenderProps = {
    className: buttonClassName,
    iconClassName,
    label: item.label,
    closeRail,
  };
  const node = item.render ? item.render(renderProps) : renderDefaultItem(item, renderProps);

  return (
    <span className="relative flex items-center" title={item.disabledReason ?? item.label}>
      {node}
    </span>
  );
}

function renderDefaultItem(
  item: WebShellRailItem,
  { className, iconClassName, label, closeRail }: WebShellRailRenderProps,
) {
  const Icon = item.icon;
  if (item.to) {
    return (
      <Link
        to={item.to}
        aria-label={label}
        data-testid={`web-shell-rail-item-${item.id}`}
        className={cn(buttonVariants({ variant: "ghost", size: "icon", className }))}
        onClick={closeRail}
      >
        <Icon className={iconClassName} />
      </Link>
    );
  }
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label={label}
      data-testid={`web-shell-rail-item-${item.id}`}
      className={className}
      onClick={() => {
        Promise.resolve()
          .then(() => item.onSelect?.())
          .catch((error: unknown) => {
            console.error(
              `WebShellRail item failed: ${item.id} (${label})`,
              error,
            );
          });
        closeRail();
      }}
    >
      <Icon className={iconClassName} />
    </Button>
  );
}
