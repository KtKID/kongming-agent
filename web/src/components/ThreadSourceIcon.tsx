import { MessageSquare } from "lucide-react";
import type { BackendKind } from "@/protocol";
import { cn } from "@/lib/utils";

type SourceVisual = {
  label: string;
  imageSrc?: string;
  icon?: typeof MessageSquare;
};

const SOURCE_VISUALS: Record<BackendKind, SourceVisual> = {
  generic_chat: { label: "普通聊天", icon: MessageSquare },
  claude_code: { label: "Claude", imageSrc: "/brand/claude-app-icon.png" },
  codex: { label: "Codex", imageSrc: "/brand/codex-app-icon.png" },
};

export function resolveThreadSourceVisual(input: {
  backendKind?: BackendKind;
  claudeThreadId?: string;
  codexThreadId?: string;
}): SourceVisual {
  if (input.claudeThreadId?.trim()) return SOURCE_VISUALS.claude_code;
  if (input.codexThreadId?.trim()) return SOURCE_VISUALS.codex;
  return SOURCE_VISUALS[input.backendKind ?? "generic_chat"];
}

interface ThreadSourceIconProps {
  backendKind?: BackendKind;
  claudeThreadId?: string;
  codexThreadId?: string;
  active?: boolean;
}

export function ThreadSourceIcon({
  backendKind,
  claudeThreadId,
  codexThreadId,
  active = false,
}: ThreadSourceIconProps) {
  const source = resolveThreadSourceVisual({
    backendKind,
    claudeThreadId,
    codexThreadId,
  });

  return (
    <span
      aria-label={`${source.label} 会话`}
      title={source.label}
      className={cn(
        "flex h-6 w-6 shrink-0 items-center justify-center rounded-xl border border-border/60 bg-background/60 text-muted-foreground shadow-sm",
        active && "border-primary/18 bg-background/80 text-foreground/90",
      )}
    >
      {source.imageSrc ? (
        <img
          src={source.imageSrc}
          alt=""
          aria-hidden="true"
          className="h-4 w-4 rounded-[4px] object-cover"
        />
      ) : source.icon ? (
        <source.icon className="h-3.5 w-3.5" aria-hidden="true" />
      ) : null}
    </span>
  );
}
