import { useState } from "react";
import { Check, ChevronUp, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { ConnectedModelFamily } from "@/modules/model-providers/types";

interface ModelSwitcherProps {
  currentPresetId?: string | null;
  options: ConnectedModelFamily[];
  disabled?: boolean;
  onSelect: (presetId: string) => void | Promise<void>;
  onManageProviders?: () => void;
}

export function ModelSwitcher({
  currentPresetId,
  options,
  disabled = false,
  onSelect,
  onManageProviders,
}: ModelSwitcherProps) {
  const [open, setOpen] = useState(false);
  const current = options.find((option) => option.presetId === currentPresetId);
  const label = current?.displayName ?? (currentPresetId ? "当前模型未连接" : "选择模型");

  const handleManageProviders = () => {
    setOpen(false);
    onManageProviders?.();
  };

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          className="h-8 max-w-[11rem] gap-1.5 truncate px-2 text-xs text-muted-foreground"
          data-testid="composer-model-switcher"
          aria-label="切换模型"
        >
          <span className="truncate">{label}</span>
          <ChevronUp className="h-3 w-3 shrink-0 opacity-50" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="top"
        align="start"
        className="w-64"
        data-testid="composer-model-menu"
      >
        <DropdownMenuItem
          asChild
          className="gap-2 text-xs"
          data-testid="composer-model-manage"
        >
          <Link to="/manage/model-providers" onClick={handleManageProviders}>
            <Settings2 className="h-3.5 w-3.5" />
            模型服务商
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {options.length === 0 ? (
          <DropdownMenuItem disabled className="text-xs text-muted-foreground">
            暂无已连接模型
          </DropdownMenuItem>
        ) : (
          options.map((option) => {
            const selected = option.presetId === currentPresetId;
            return (
              <DropdownMenuItem
                key={option.familyId}
                onSelect={() => void onSelect(option.presetId)}
                className="flex items-center justify-between gap-3 text-xs"
                data-testid={`composer-model-option-${option.providerId}`}
              >
                <span className="min-w-0 truncate">{option.displayName}</span>
                <Check
                  data-testid={`composer-model-check-${option.providerId}`}
                  className={cn(
                    "h-3.5 w-3.5 shrink-0",
                    selected ? "opacity-100" : "opacity-0",
                  )}
                />
              </DropdownMenuItem>
            );
          })
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
