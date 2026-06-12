import { Ban, Check, Folder, FolderPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { pickDirectory } from "@/lib/dirPicker";
import { isAbsoluteProjectPath } from "@/lib/path";
import type { ThreadMetadataDTO } from "@/protocol";

export interface ThreadProjectOption {
  cwd: string;
  label: string;
  title: string;
  threadCount: number;
  source: "existing_thread" | "file_picker" | "none";
}

interface ProjectBucket {
  option: ThreadProjectOption;
  latestUpdatedAt: number;
}

function normalizeThreadProjectCwd(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  if (trimmed === "/") return "/";
  if (/^[A-Za-z]:[\\/]$/.test(trimmed)) return trimmed;
  if (/^\\\\[^\\]+\\[^\\]+[\\/]?$/.test(trimmed)) return trimmed.replace(/[\\/]$/, "");
  return trimmed.replace(/[\\/]+$/, "");
}

function projectKey(cwd: string): string {
  const normalized = normalizeThreadProjectCwd(cwd);
  if (/^[A-Za-z]:[\\/]/.test(normalized) || /^\\\\/.test(normalized)) {
    return normalized.replace(/\\/g, "/").toLowerCase();
  }
  return normalized;
}

function basename(cwd: string): string {
  const normalized = normalizeThreadProjectCwd(cwd);
  const pieces = normalized.split(/[\\/]/).filter(Boolean);
  return pieces[pieces.length - 1] ?? normalized;
}

export function deriveThreadProjectOptions(
  threads: ThreadMetadataDTO[],
): ThreadProjectOption[] {
  const buckets = new Map<string, ProjectBucket>();
  for (const thread of threads) {
    const cwd = normalizeThreadProjectCwd(thread.cwd ?? "");
    if (!cwd || !isAbsoluteProjectPath(cwd)) continue;
    const key = projectKey(cwd);
    const existing = buckets.get(key);
    if (existing) {
      existing.option.threadCount += 1;
      existing.latestUpdatedAt = Math.max(existing.latestUpdatedAt, thread.updated_at);
      continue;
    }
    buckets.set(key, {
      latestUpdatedAt: thread.updated_at,
      option: {
        cwd,
        label: basename(cwd),
        title: cwd,
        threadCount: 1,
        source: "existing_thread",
      },
    });
  }
  return [...buckets.values()]
    .sort((a, b) => b.latestUpdatedAt - a.latestUpdatedAt)
    .map((bucket) => bucket.option);
}

export function noneProjectOption(): ThreadProjectOption {
  return {
    cwd: "",
    label: "不需要项目",
    title: "不绑定项目目录",
    threadCount: 0,
    source: "none",
  };
}

function filePickerProjectOption(cwd: string): ThreadProjectOption {
  const normalized = normalizeThreadProjectCwd(cwd);
  return {
    cwd: normalized,
    label: basename(normalized),
    title: normalized,
    threadCount: 0,
    source: "file_picker",
  };
}

export function ThreadProjectSelector({
  threads,
  value,
  onChange,
  disabled = false,
}: {
  threads: ThreadMetadataDTO[];
  value: ThreadProjectOption | null;
  onChange: (option: ThreadProjectOption | null) => void;
  disabled?: boolean;
}) {
  const options = deriveThreadProjectOptions(threads);
  const selectedLabel = value?.label ?? "选择项目";
  const selectedTitle = value?.title ?? "未选择项目时使用用户 home";

  const selectNewProject = async () => {
    const picked = await pickDirectory({
      title: "选择新项目",
      defaultPath: value?.cwd || undefined,
    });
    if (!picked) return;
    const normalized = normalizeThreadProjectCwd(picked);
    if (!isAbsoluteProjectPath(normalized)) return;
    onChange(filePickerProjectOption(normalized));
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          title={selectedTitle}
          data-testid="thread-project-selector-trigger"
          className="max-w-[16rem] gap-1.5 truncate text-xs text-muted-foreground"
        >
          <Folder className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{selectedLabel}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="end" className="w-72">
        {options.map((option) => {
          const active =
            value !== null && value.cwd === option.cwd && value.source !== "none";
          return (
            <DropdownMenuItem
              key={option.cwd}
              onClick={() => onChange(option)}
              title={option.title}
              data-testid="thread-project-option"
              className="items-start gap-2"
            >
              <Folder className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs font-medium">{option.label}</span>
                <span className="block truncate text-[11px] text-muted-foreground">
                  {option.title}
                  {option.threadCount > 1 ? ` · ${option.threadCount} 个会话` : ""}
                </span>
              </span>
              {active ? <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
            </DropdownMenuItem>
          );
        })}
        {options.length > 0 ? <DropdownMenuSeparator /> : null}
        <DropdownMenuItem
          onClick={() => void selectNewProject()}
          data-testid="thread-project-pick-directory"
          className="gap-2 text-xs"
        >
          <FolderPlus className="h-3.5 w-3.5" />
          选择新项目
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={() => onChange(noneProjectOption())}
          data-testid="thread-project-none"
          className="gap-2 text-xs"
        >
          <Ban className="h-3.5 w-3.5" />
          不需要项目
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
