import { Moon, Sun, Monitor } from "lucide-react";
import { useThemeStore } from "@/stores/theme";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const ICONS = { light: Sun, dark: Moon, system: Monitor } as const;
const LABELS = { light: "古风亮", dark: "黑曜暗", system: "跟随系统" } as const;

export function ThemeToggle() {
  const { theme, setTheme } = useThemeStore();
  const Icon = ICONS[theme];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" aria-label="切换主题">
          <Icon className="h-3.5 w-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {(Object.keys(ICONS) as Array<"light" | "dark" | "system">).map(
          (key) => {
            const ItemIcon = ICONS[key];
            return (
              <DropdownMenuItem
                key={key}
                onClick={() => setTheme(key)}
                className={theme === key ? "bg-accent text-accent-foreground" : ""}
              >
                <ItemIcon className="mr-2 h-4 w-4" />
                {LABELS[key]}
              </DropdownMenuItem>
            );
          },
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
