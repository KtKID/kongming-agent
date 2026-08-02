import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Send, Brain, ChevronUp, Copy, Paperclip, Plus, Sparkles, Square, X } from "lucide-react";
import { StatusLine } from "@/components/StatusLine";
import { SlashMenu, useSlashMenu, type SlashCatalogItem } from "@/components/SlashMenu";
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
import { ConversationReferenceManager } from "@/modules/conversation-references/ConversationReferenceManager";
import type {
  ConversationReferenceDTO,
  UserInputAttachment,
} from "@/protocol";

export type ReasoningEffort = "none" | "low" | "medium" | "high" | "max";

/**
 * 最近一次已提交并被清空的草稿快照。
 *
 * pending queue 的拒绝帧可能晚于 UI 清空动作到达；该结构保存恢复所需的
 * 用户可见文本、reasoning 设置、ready 附件和引用。
 */
export interface SubmittedDraft {
  /** 用户点击发送时的原始输入框文本；异步拒绝后按原样恢复。 */
  text: string;
  /** 当次发送使用的 reasoning 设置；恢复草稿时同步恢复。 */
  reasoningEffort: ReasoningEffort | null;
  /** 当次发送时已上传完成的附件；上传中和失败附件不会进入草稿。 */
  attachments: UserInputAttachment[];
  /** 当次发送时的会话引用；恢复草稿时重新挂回 composer。 */
  references: ConversationReferenceDTO[];
}

interface ReasoningOption {
  label: string;
  value: ReasoningEffort | null;
}

const REASONING_OPTIONS: ReasoningOption[] = [
  { label: "关闭", value: "none" },
  { label: "低", value: "low" },
  { label: "中", value: "medium" },
  { label: "高", value: "high" },
  { label: "最高", value: "max" },
];

function isReasoningEnabled(value: ReasoningEffort | null): boolean {
  return value !== null && value !== "none";
}

interface ComposerProps {
  /** 是否禁用输入（连接未就绪等） */
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
    references?: ConversationReferenceDTO[],
    submittedDraft?: SubmittedDraft,
  ) => void | boolean | Promise<void | boolean>;
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
   * 调用方传 `<AutoApprovalModeSelector cwd={...} socket={...} />` 之类的组件；
   * 本 Composer 不感知具体业务，仅提供位置。generic_chat 通道不传则不显示。
   */
  leftActions?: ReactNode;
  /** 深度思考右侧的模型切换控件。 */
  modelSwitcher?: ReactNode;
  /** 当前模型支持的 reasoning 选项；未传时展示全量中间层档位。 */
  reasoningOptions?: ReasoningEffort[];
  /** 当前模型的 catalog 默认 reasoning；模型切换时同步更新选择。 */
  defaultReasoningEffort?: ReasoningEffort | null;
  /** reasoning 选择所属的模型 identity；identity 变化时采用新模型默认档位。 */
  reasoningSelectionKey?: string | null;
  /** 组件重挂载时恢复的用户显式选择。 */
  initialReasoningEffort?: ReasoningEffort | null;
  /**
   * 外部发送链失败后需要恢复的完整草稿。
   *
   * 仅在当前输入为空且 seed 发生变化时写入，避免覆盖用户已经继续编辑的内容。
   */
  draftSeed?: SubmittedDraft | null;
  /** 用户显式调整 reasoning 档位后的通知。 */
  onReasoningEffortChange?: (effort: ReasoningEffort | null) => void;
  /**
   * interrupt-run-v0.1：当前是否有 active run 可中断。
   *
   * 父组件计算（典型 = `lastAssistantStreaming`）；运行中继续提交会进入后端队列。
   */
  isRunning?: boolean;
  /** 运行中是否保留发送按钮；generic_chat 使用它把后续输入提交到队列。 */
  allowSubmitWhileRunning?: boolean;
  /**
   * 父组件收到异步拒绝（如 pending_input_queue_full）时递增该 token，
   * Composer 会恢复最近一次已提交并已清空的草稿。
   */
  restoreDraftToken?: number | null;
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
 * 连接不可用时（disabled）禁用 textarea + 按钮 + 占位文案变更。
 */
export function Composer({
  disabled = false,
  onSubmit,
  softLimit = 8000,
  threadId,
  leftActions,
  modelSwitcher,
  reasoningOptions,
  defaultReasoningEffort,
  reasoningSelectionKey = null,
  initialReasoningEffort,
  draftSeed = null,
  onReasoningEffortChange,
  isRunning = false,
  allowSubmitWhileRunning = false,
  restoreDraftToken = null,
  onInterrupt,
}: ComposerProps) {
  // interrupt-run-v0.1：有 active run + 父组件给了回调时显示 Stop 按钮。
  const showStopButton = isRunning && typeof onInterrupt === "function";
  const showSendButton = !showStopButton || allowSubmitWhileRunning;
  const [value, setValue] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<
    ReasoningEffort | null
  >(initialReasoningEffort ?? null);
  const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);
  const [references, setReferences] = useState<ConversationReferenceDTO[]>([]);
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const lastSubmittedDraftRef = useRef<SubmittedDraft | null>(null);
  const lastRestoreDraftTokenRef = useRef<number | null>(null);
  const lastDraftSeedRef = useRef<SubmittedDraft | null>(null);
  const reasoningSelectionInitializedRef = useRef(false);
  const reasoningSelectionKeyRef = useRef<string | null>(reasoningSelectionKey);
  const reasoningSelectionExplicitRef = useRef(initialReasoningEffort !== undefined);
  const slashMenu = useSlashMenu(threadId);
  const { showMenu, entries: menuEntries, handleInputChange } = slashMenu;
  const [menuActiveIndex, setMenuActiveIndex] = useState(0);
  const reasoningControlVisible = reasoningOptions === undefined || reasoningOptions.length > 0;

  const availableReasoningOptions = REASONING_OPTIONS.filter((opt) => {
    if (reasoningOptions === undefined) return true;
    return reasoningOptions.includes(opt.value ?? "none");
  });

  useEffect(() => {
    if (reasoningOptions === undefined) return;
    const initialized = reasoningSelectionInitializedRef.current;
    const selectionKeyChanged =
      initialized && reasoningSelectionKeyRef.current !== reasoningSelectionKey;
    const catalogDefault = defaultReasoningEffort ?? "none";
    const nextDefault = reasoningOptions.includes(catalogDefault)
      ? catalogDefault
      : (reasoningOptions[0] ?? null);

    setReasoningEffort((current) => {
      if (reasoningOptions.length === 0) {
        reasoningSelectionExplicitRef.current = false;
        return null;
      }
      if (!initialized) {
        if (
          initialReasoningEffort !== undefined &&
          initialReasoningEffort !== null &&
          reasoningOptions.includes(initialReasoningEffort)
        ) {
          reasoningSelectionExplicitRef.current = true;
          return initialReasoningEffort;
        }
        reasoningSelectionExplicitRef.current = false;
        return nextDefault;
      }
      if (selectionKeyChanged) {
        reasoningSelectionExplicitRef.current = false;
        return nextDefault;
      }
      if (current === null || !reasoningOptions.includes(current)) {
        reasoningSelectionExplicitRef.current = false;
        return nextDefault;
      }
      if (!reasoningSelectionExplicitRef.current) {
        return nextDefault;
      }
      return current;
    });
    reasoningSelectionInitializedRef.current = true;
    reasoningSelectionKeyRef.current = reasoningSelectionKey;
  }, [
    defaultReasoningEffort,
    initialReasoningEffort,
    reasoningOptions,
    reasoningSelectionKey,
  ]);

  // 附件上传（粘贴图片 + 📎 按钮选文件，共用同一 hook）
  const uploader = useAttachmentUploader();
  const { restoreReadyAttachments } = uploader;
  const { onPaste } = usePasteAttachments({
    disabled: disabled || !threadId,
    onImagePaste: (file) => {
      if (threadId) uploader.upload(file, threadId);
    },
  });

  useEffect(() => {
    if (!draftSeed || lastDraftSeedRef.current === draftSeed) return;
    lastDraftSeedRef.current = draftSeed;
    const hasCurrentDraft =
      value.trim().length > 0 ||
      references.length > 0 ||
      uploader.attachments.length > 0;
    if (hasCurrentDraft) return;
    setValue(draftSeed.text);
    reasoningSelectionExplicitRef.current = true;
    setReasoningEffort(draftSeed.reasoningEffort);
    onReasoningEffortChange?.(draftSeed.reasoningEffort);
    setReferences(draftSeed.references);
    restoreReadyAttachments(draftSeed.attachments);
    ref.current?.focus();
  }, [
    draftSeed,
    onReasoningEffortChange,
    references.length,
    restoreReadyAttachments,
    uploader.attachments.length,
    value,
  ]);

  useEffect(() => {
    // pending input queue 异步拒绝发生在 Composer 已清空之后；父组件通过
    // restoreDraftToken 通知这里恢复最近一次提交草稿。若用户已经继续输入新内容，
    // 保留当前输入，避免旧草稿覆盖用户后续编辑。
    if (
      restoreDraftToken === null ||
      lastRestoreDraftTokenRef.current === restoreDraftToken
    ) {
      return;
    }
    lastRestoreDraftTokenRef.current = restoreDraftToken;
    const draft = lastSubmittedDraftRef.current;
    if (!draft) return;
    const hasCurrentDraft =
      value.trim().length > 0 ||
      references.length > 0 ||
      uploader.attachments.length > 0;
    if (hasCurrentDraft) return;
    setValue(draft.text);
    reasoningSelectionExplicitRef.current = true;
    setReasoningEffort(draft.reasoningEffort);
    onReasoningEffortChange?.(draft.reasoningEffort);
    setReferences(draft.references);
    restoreReadyAttachments(draft.attachments);
    ref.current?.focus();
  }, [
    references.length,
    restoreReadyAttachments,
    restoreDraftToken,
    onReasoningEffortChange,
    uploader.attachments.length,
    value,
  ]);

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
      setReferences([]);
      slashMenu.closeMenu();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  const submit = async () => {
    const text = value.trim();
    const ready = uploader.readyAttachments;
    // 允许"只有图片没文字"或"只有文字没图片"或"图片+文字"或"只有引用"
    if (disabled) return;
    if (text.length === 0 && ready.length === 0 && references.length === 0) return;
    if (uploader.hasUploading) return;
    const expandedText = ConversationReferenceManager.prependPromptInjectedReferences(
      text,
      references,
    );
    const passthroughReferences =
      ConversationReferenceManager.passthroughReferences(references);
    // 先记录草稿再调用 onSubmit；后端异步返回 pending_input_queue_full 时，
    // 父组件递增 restoreDraftToken，Composer 可恢复刚刚被清空的输入和附件。
    const submittedDraft: SubmittedDraft = {
      text: value,
      reasoningEffort,
      attachments: ready,
      references,
    };
    lastSubmittedDraftRef.current = submittedDraft;
    const shouldClear = await onSubmit(
      expandedText,
      reasoningEffort,
      ready.length > 0 ? ready : undefined,
      passthroughReferences.length > 0 ? passthroughReferences : undefined,
      submittedDraft,
    );
    if (shouldClear === false) return;
    setValue("");
    setReferences([]);
    uploader.clear();
    slashMenu.closeMenu();
  };

  const handleSlashSelect = (item: SlashCatalogItem) => {
    if (item.action === "bind_reference" && item.reference_template) {
      const nextReference = ConversationReferenceManager.createFromTemplate(
        item.reference_template,
        item.id,
      );
      setReferences((prev) =>
        ConversationReferenceManager.hasSameReference(prev, nextReference)
          ? prev
          : [...prev, nextReference],
      );
      setValue("");
      slashMenu.closeMenu();
      ref.current?.focus();
      return;
    }
    const nextText = item.insert_text ?? (item.slash ? `${item.slash} ` : "");
    if (!nextText) return;
    setValue(nextText);
    slashMenu.closeMenu();
    ref.current?.focus();
  };

  const handleChange = (text: string) => {
    setValue(text);
    handleInputChange(text);
  };

  const copyReference = (reference: ConversationReferenceDTO) => {
    const clipboard = navigator.clipboard;
    if (!clipboard) return;
    void clipboard
      .writeText(ConversationReferenceManager.toClipboardText(reference))
      .catch(() => undefined);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showMenu && menuEntries.length > 0) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenuActiveIndex((menuActiveIndex - 1 + menuEntries.length) % menuEntries.length);
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenuActiveIndex((menuActiveIndex + 1) % menuEntries.length);
        return;
      }
      if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        const item = slashMenu.activateEntry(menuEntries[menuActiveIndex]);
        if (item) handleSlashSelect(item);
        setMenuActiveIndex(0);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        slashMenu.closeMenu();
        setMenuActiveIndex(0);
        return;
      }
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void submit();
    }
  };

  const overflow = value.length > softLimit;
  const activeLabel =
    availableReasoningOptions.find((o) => o.value === reasoningEffort)?.label ?? "关闭";
  const sendLabel =
    uploader.hasUploading ? "上传中" : isRunning && allowSubmitWhileRunning ? "排队" : "发送";

  useEffect(() => {
    setMenuActiveIndex(0);
  }, [menuEntries]);

  return (
    <div className="border-t border-border/60 bg-background/10 p-2">
      <div className="w-full">
        <div className="flex w-full flex-col gap-3">
          <div className="relative">
          <SlashMenu
            entries={menuEntries}
            onActivate={(entry) => {
              const item = slashMenu.activateEntry(entry);
              if (item) handleSlashSelect(item);
              setMenuActiveIndex(0);
            }}
            onClose={slashMenu.closeMenu}
            visible={showMenu}
            activeIndex={menuActiveIndex}
          />
          {references.length > 0 ? (
            <div
              className="mb-2 flex flex-wrap items-center gap-1.5"
              data-testid="composer-reference-row"
            >
              {references.map((reference) => (
                <span
                  key={reference.id}
                  className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-primary/25 bg-primary/10 px-2 py-1 text-xs font-medium text-primary"
                  data-testid="composer-reference-chip"
                  title={`${reference.label} - ${reference.ref}`}
                >
                  <Sparkles className="h-3.5 w-3.5 shrink-0" />
                  <span className="max-w-[12rem] truncate">{reference.label}</span>
                  <button
                    type="button"
                    className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-primary/70 hover:bg-primary/15 hover:text-primary"
                    onClick={() => copyReference(reference)}
                    aria-label={`复制引用 ${reference.label}`}
                    title="复制引用"
                  >
                    <Copy className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-primary/70 hover:bg-primary/15 hover:text-primary disabled:opacity-50"
                    onClick={() =>
                      setReferences((prev) =>
                        prev.filter((item) => item.id !== reference.id),
                      )
                    }
                    disabled={disabled}
                    aria-label={`移除引用 ${reference.label}`}
                    title="移除引用"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <Textarea
            ref={ref}
            value={value}
            rows={COMPOSER_TEXTAREA_MIN_ROWS}
            disabled={disabled}
            onChange={(e) => handleChange(e.target.value)}
            onKeyDown={onKey}
            onPaste={onPaste}
            placeholder={
              disabled
                ? "连接未就绪"
                : isRunning && allowSubmitWhileRunning
                  ? "继续输入以排队后续消息"
                  : "输入消息（⌘⏎ 发送），/ 打开命令菜单"
            }
            className="min-h-0 resize-none overflow-y-hidden border-input/85 bg-card/82 py-0.5 text-card-foreground shadow-none leading-5 placeholder:text-muted-foreground/80 focus-visible:border-primary/45 focus-visible:ring-primary/35 dark:border-border/90 dark:bg-background/72 dark:text-foreground dark:placeholder:text-muted-foreground/72 dark:focus-visible:border-primary/55 dark:focus-visible:ring-primary/45"
            aria-label="消息输入"
          />
          <ThumbnailStrip
            attachments={uploader.attachments}
            onRemove={uploader.remove}
            className="mt-2"
          />
        </div>
        <div
          className="flex items-center justify-between gap-3"
          data-testid="composer-action-row"
        >
          <div
            className="flex min-w-0 flex-1 flex-wrap items-center gap-1"
            data-testid="composer-primary-actions"
          >
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
            {reasoningControlVisible ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={disabled}
                    className={[
                      "gap-1.5 text-xs text-muted-foreground",
                      isReasoningEnabled(reasoningEffort) && "text-primary",
                    ].join(" ")}
                  >
                    <Brain className="h-3.5 w-3.5" />
                    深度思考
                    {isReasoningEnabled(reasoningEffort) && (
                      <span className="rounded bg-primary/10 px-1 py-0.5 text-[10px] font-medium text-primary">
                        {activeLabel}
                      </span>
                    )}
                    <ChevronUp className="h-3 w-3 opacity-50" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent side="top" align="start" className="w-32">
                  <DropdownMenuRadioGroup
                    value={reasoningEffort ?? "none"}
                    onValueChange={(v) => {
                      const nextEffort = v as ReasoningEffort;
                      reasoningSelectionExplicitRef.current = true;
                      setReasoningEffort(nextEffort);
                      onReasoningEffortChange?.(nextEffort);
                    }}
                  >
                    {availableReasoningOptions.map((opt) => (
                      <DropdownMenuRadioItem
                        key={opt.label}
                        value={opt.value ?? "none"}
                        className="text-xs"
                      >
                        {opt.label}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
            {modelSwitcher}
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
          <div
            className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground"
            data-testid="composer-secondary-actions"
          >
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
            ) : null}
            {showSendButton ? (
              <Button
                type="button"
                size="sm"
                onClick={() => void submit()}
                disabled={
                  disabled ||
                  uploader.hasUploading ||
                  (value.trim().length === 0 &&
                    uploader.readyAttachments.length === 0 &&
                    references.length === 0)
                }
                className="min-w-[5.5rem] border-primary/30 bg-primary text-primary-foreground hover:bg-primary/92"
                data-testid="composer-send"
              >
                <Send className="h-3.5 w-3.5" />
                {sendLabel}
              </Button>
            ) : null}
          </div>
        </div>
        <StatusLine threadId={threadId} reasoningEffort={reasoningEffort} />
        </div>
      </div>
    </div>
  );
}
