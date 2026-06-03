import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Send, Brain, ChevronUp, Paperclip, Plus, Square } from "lucide-react";
import { StatusLine } from "@/components/StatusLine";
import { SlashMenu, useSlashMenu, type SlashCandidate } from "@/components/SlashMenu";
import { ThumbnailStrip } from "@/components/ThumbnailStrip";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from "@/components/ui/dropdown-menu";
import { useAttachmentUploader } from "@/hooks/useAttachmentUploader";
import { usePasteAttachments } from "@/hooks/usePasteAttachments";
import {
  COMPOSER_TEXTAREA_MIN_ROWS,
  resizeComposerTextarea,
} from "@/lib/composer-textarea";
import type { UserInputAttachment } from "@/protocol";

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
  /**
   * 发送回调。
   *
   * attachments 仅含 ``status="ready"`` 的资产引用；上传中或失败的附件不会进入此参数。
   */
  onSubmit: (
    text: string,
    reasoningEffort: ReasoningEffort | null,
    attachments?: UserInputAttachment[],
  ) => void;
  /** 软上限；超出仍可发，但显示提醒 */
  softLimit?: number;
  /**
   * 当前 thread ID。
   *
   * - 传给 StatusLine 显示 token 用量
   * - 上传图片时作为 form ``thread_id`` 发给 ``POST /api/uploads/images``
   * - 未提供时图片粘贴被吞掉（hook 内 disabled），避免无目标 thread 的孤儿上传
   */
  threadId?: string;
  /**
   * 发送按钮**左侧**的扩展槽（v0.x smart-approval-v1 用）。
   *
   * 调用方传 `<AutoApprovalToggle cwd={...} socket={...} />` 之类的组件；
   * 本 Composer 不感知具体业务，仅提供位置。generic_chat 通道不传则不显示。
   */
  leftActions?: ReactNode;
  /**
   * interrupt-run-v0.1：当前是否有 active run 可中断。
   *
   * 父组件计算（典型 = `lastAssistantStreaming`）；
   * 与 `disabled` 结合：
   * - `disabled=true && isRunning=true && onInterrupt`：发送按钮替换为 Stop 按钮
   * - 其它情况：保持原行为（发送按钮 disabled / 可点）
   */
  isRunning?: boolean;
  /**
   * interrupt-run-v0.1：用户点 Stop 按钮回调。
   *
   * 父组件负责往 socket 发 `{frame_type: "interrupt"}` 帧。
   * 未传则不显示 Stop 按钮（保持原 disabled 状态的 Send 按钮）。
   */
  onInterrupt?: () => void;
}

/**
 * 输入框：自适应高度 + ⌘⏎ / Ctrl⏎ 发送 + 字符计数 + 思考模式 toggle + 图片粘贴上传。
 *
 * 推理中（disabled）禁用 textarea + 按钮 + 占位文案变更，图片粘贴也被吞掉。
 */
export function Composer({
  disabled = false,
  onSubmit,
  softLimit = 8000,
  threadId,
  leftActions,
  isRunning = false,
  onInterrupt,
}: ComposerProps) {
  // interrupt-run-v0.1：disabled + 有 active run + 父组件给了回调时，
  // 把"发送"按钮换成 Stop 按钮；其它情况不变。
  const showStopButton = disabled && isRunning && typeof onInterrupt === "function";
  const [value, setValue] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<
    ReasoningEffort | null
  >(null);
  const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const { showMenu, slashQuery, setShowMenu, handleInputChange } = useSlashMenu();
  const [menuActiveIndex, setMenuActiveIndex] = useState(0);
  const [menuFiltered, setMenuFiltered] = useState<SlashCandidate[]>([]);

  // 附件上传（粘贴图片 + 📎 按钮选文件，共用同一 hook）
  const uploader = useAttachmentUploader();
  const { onPaste } = usePasteAttachments({
    disabled: disabled || !threadId,
    onImagePaste: (file) => {
      if (threadId) uploader.upload(file, threadId);
    },
  });

  // 📎 按钮 → 触发隐藏 <input type=file>
  const openFilePicker = () => {
    if (disabled || !threadId) return;
    fileInputRef.current?.click();
  };

  const openImagePickerFromMenu = () => {
    setAttachmentMenuOpen(false);
    window.setTimeout(() => {
      openFilePicker();
    }, 0);
  };

  // 文件选择后：遍历选中的图片走同一条 uploader.upload 链路
  const onFilesChosen = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || !threadId) return;
    for (const file of Array.from(files)) {
      uploader.upload(file, threadId);
    }
    // 清空 input.value，让用户能"再选同一张图"也触发 onChange
    e.target.value = "";
  };

  // 输入框保持两行起步，第三行开始向上增长，六行封顶后内部滚动。
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    resizeComposerTextarea(el);
  }, [value]);

  // 切换 thread → 清空 uploader + textarea
  //
  // 防止跨 thread asset 污染（前置 task R2 fix 补的）。
  //
  // 关键：用 ref 持上一个 threadId，**只在 threadId "实际变化" 时**触发清空。
  // 不能依赖 useEffect [threadId] 的"依赖 diff"，因为父组件 re-render 可能
  // 传同样的 threadId，但 effect cleanup + re-run 期间可能误触发 store
  // (uploader 内 setAttachments) 形成 React #185 死循环。
  const lastThreadIdRef = useRef<string | undefined>(threadId);
  useEffect(() => {
    if (lastThreadIdRef.current !== threadId) {
      lastThreadIdRef.current = threadId;
      uploader.clear();
      setValue("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  const submit = () => {
    const text = value.trim();
    const ready = uploader.readyAttachments;
    // 允许"只有图片没文字"或"只有文字没图片"或"图片+文字"
    if (disabled) return;
    if (text.length === 0 && ready.length === 0) return;
    if (uploader.hasUploading) return;
    onSubmit(text, reasoningEffort, ready.length > 0 ? ready : undefined);
    setValue("");
    uploader.clear();
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
    <div className="border-t border-border/60 bg-background/10 p-2">
      <div className="w-full">
        <div className="flex w-full flex-col gap-3">
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
            rows={COMPOSER_TEXTAREA_MIN_ROWS}
            disabled={disabled}
            onChange={(e) => handleChange(e.target.value)}
            onKeyDown={onKey}
            onPaste={onPaste}
            placeholder={
              disabled ? "推理中，请稍候..." : "输入消息（⌘⏎ 发送），/ 打开命令菜单"
            }
            className="min-h-0 resize-none overflow-y-hidden border-border/70 bg-background/88 py-0 shadow-none leading-5"
            aria-label="消息输入"
          />
          <ThumbnailStrip
            attachments={uploader.attachments}
            onRemove={uploader.remove}
            className="mt-2"
          />
        </div>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-1">
            <DropdownMenu
              open={attachmentMenuOpen}
              onOpenChange={setAttachmentMenuOpen}
            >
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={disabled || !threadId || uploader.hasUploading}
                  className="h-8 w-8 text-muted-foreground"
                  data-testid="composer-plus-trigger"
                  aria-label="打开附加操作"
                >
                  <Plus className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" align="start" className="w-32">
                <DropdownMenuItem
                  onClick={openImagePickerFromMenu}
                  className="gap-2 text-xs"
                  data-testid="composer-attach-button"
                >
                  <Paperclip className="h-3.5 w-3.5" />
                  图片
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
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
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            multiple
            hidden
            onChange={onFilesChosen}
            data-testid="composer-file-input"
          />
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className={overflow ? "text-destructive" : ""}>
              {value.length}
              {overflow ? ` / 软上限 ${softLimit}` : ""}
            </span>
            {leftActions /* 发送按钮左侧扩展槽（v0.x smart-approval-v1） */}
            {showStopButton ? (
              <Button
                type="button"
                size="sm"
                onClick={onInterrupt}
                className="min-w-[5.5rem] border-destructive/30 bg-destructive text-destructive-foreground hover:bg-destructive/92"
                data-testid="composer-stop"
                aria-label="停止当前任务"
              >
                <Square className="h-3.5 w-3.5" />
                停止
              </Button>
            ) : (
              <Button
                type="button"
                size="sm"
                onClick={submit}
                disabled={
                  disabled ||
                  uploader.hasUploading ||
                  (value.trim().length === 0 &&
                    uploader.readyAttachments.length === 0)
                }
                className="min-w-[5.5rem] border-primary/30 bg-primary text-primary-foreground hover:bg-primary/92"
                data-testid="composer-send"
              >
                <Send className="h-3.5 w-3.5" />
                {uploader.hasUploading ? "上传中" : "发送"}
              </Button>
            )}
          </div>
        </div>
        <StatusLine threadId={threadId} reasoningEffort={reasoningEffort} />
        </div>
      </div>
    </div>
  );
}
