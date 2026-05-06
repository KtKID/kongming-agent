import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Send, Brain, ChevronUp } from "lucide-react";
import { StatusLine } from "@/components/StatusLine";
import { SlashMenu, useSlashMenu, type SlashCandidate } from "@/components/SlashMenu";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "@/components/ui/dropdown-menu";

export type ReasoningEffort = "low" | "medium" | "high";

interface ReasoningOption {
  label: string;
  value: ReasoningEffort | null;
}

const REASONING_OPTIONS: ReasoningOption[] = [
  { label: "关闭", value: null },
  { label: "低", value: "low" },
  { label: "中", value: "medium" },
  { label: "高", value: "high" },
];

interface ComposerProps {
  /** 是否禁用输入（推理中等） */
  disabled?: boolean;
  onSubmit: (text: string, reasoningEffort: ReasoningEffort | null) => void;
  /** 软上限；超出仍可发，但显示提醒 */
  softLimit?: number;
  /** 当前 thread ID，传给 StatusLine 显示 token 用量 */
  threadId?: string;
}

/**
 * 输入框：自适应高度 + ⌘⏎ / Ctrl⏎ 发送 + 字符计数 + 思考模式 toggle。
 *
 * 推理中（disabled）禁用 textarea + 按钮 + 占位文案变更。
 */
export function Composer({
  disabled = false,
  onSubmit,
  softLimit = 8000,
  threadId,
}: ComposerProps) {
  const [value, setValue] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<
    ReasoningEffort | null
  >(null);
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const { showMenu, slashQuery, setShowMenu, handleInputChange } = useSlashMenu();
  const [menuActiveIndex, setMenuActiveIndex] = useState(0);
  const [menuFiltered, setMenuFiltered] = useState<SlashCandidate[]>([]);

  // 自适应高度
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [value]);

  const submit = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSubmit(text, reasoningEffort);
    setValue("");
    setShowMenu(false);
  };

  const handleSlashSelect = (candidate: SlashCandidate) => {
    setValue(candidate.slash + " ");
    setShowMenu(false);
    ref.current?.focus();
  };

  const handleChange = (text: string) => {
    setValue(text);
    handleInputChange(text);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showMenu && menuFiltered.length > 0) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenuActiveIndex((menuActiveIndex - 1 + menuFiltered.length) % menuFiltered.length);
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenuActiveIndex((menuActiveIndex + 1) % menuFiltered.length);
        return;
      }
      if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        handleSlashSelect(menuFiltered[menuActiveIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowMenu(false);
        return;
      }
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  const overflow = value.length > softLimit;
  const activeLabel =
    REASONING_OPTIONS.find((o) => o.value === reasoningEffort)?.label ?? "关闭";

  return (
    <div className="border-t border-border bg-background p-4">
      <div className="mx-auto flex max-w-3xl flex-col gap-2">
        <div className="relative">
          <SlashMenu
            query={slashQuery}
            onSelect={handleSlashSelect}
            onClose={() => setShowMenu(false)}
            visible={showMenu}
            onFilteredChange={(f) => { setMenuFiltered(f); setMenuActiveIndex(0); }}
            activeIndex={menuActiveIndex}
          />
          <Textarea
            ref={ref}
            value={value}
            disabled={disabled}
            onChange={(e) => handleChange(e.target.value)}
            onKeyDown={onKey}
            placeholder={
              disabled ? "推理中，请稍候..." : "输入消息（⌘⏎ 发送），/ 打开命令菜单"
            }
            className="resize-none"
            aria-label="消息输入"
          />
        </div>
        <div className="flex items-center justify-between">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                disabled={disabled}
                className={[
                  "gap-1.5 text-xs text-muted-foreground",
                  reasoningEffort !== null && "text-primary",
                ].join(" ")}
              >
                <Brain className="h-3.5 w-3.5" />
                深度思考
                {reasoningEffort !== null && (
                  <span className="rounded bg-primary/10 px-1 py-0.5 text-[10px] font-medium text-primary">
                    {activeLabel}
                  </span>
                )}
                <ChevronUp className="h-3 w-3 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent side="top" align="start" className="w-32">
              <DropdownMenuRadioGroup
                value={reasoningEffort ?? "off"}
                onValueChange={(v) =>
                  setReasoningEffort(v === "off" ? null : (v as ReasoningEffort))
                }
              >
                {REASONING_OPTIONS.map((opt) => (
                  <DropdownMenuRadioItem
                    key={opt.label}
                    value={opt.value ?? "off"}
                    className="text-xs"
                  >
                    {opt.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className={overflow ? "text-destructive" : ""}>
              {value.length}
              {overflow ? ` / 软上限 ${softLimit}` : ""}
            </span>
            <Button
              type="button"
              size="sm"
              onClick={submit}
              disabled={disabled || value.trim().length === 0}
            >
              <Send className="h-3.5 w-3.5" />
              发送
            </Button>
          </div>
        </div>
        <StatusLine threadId={threadId} reasoningEffort={reasoningEffort} />
      </div>
    </div>
  );
}
