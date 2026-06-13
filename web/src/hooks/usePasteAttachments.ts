import { useCallback, type ClipboardEvent } from "react";

export interface UsePasteAttachmentsOptions {
  /** 检测到 image/* 时回调；多张图片会逐个调用 */
  onImagePaste: (file: File) => void;
  /** 是否禁用粘贴拦截（如推理中） */
  disabled?: boolean;
}

export interface UsePasteAttachmentsReturn {
  /** 给 textarea 的 onPaste handler */
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
}

/**
 * Textarea 粘贴拦截 hook：检测 clipboardData 中的图片项，
 * 命中至少一张图片时 preventDefault 并对每张图片调用 `onImagePaste`；
 * 否则不拦截，让浏览器默认插入文本。
 *
 * 关键边界：
 * - `disabled === true` 时直接 return，原始粘贴行为不变。
 * - 不读取文件字节 / 不调用 toast；文件读取与错误处理由上层 uploader 负责。
 * - 一次 paste 事件 clipboardData 要么全文本要么含图片，含图片时图片优先拦截。
 */
export function usePasteAttachments(
  options: UsePasteAttachmentsOptions,
): UsePasteAttachmentsReturn {
  const { onImagePaste, disabled } = options;

  const onPaste = useCallback(
    (event: ClipboardEvent<HTMLTextAreaElement>): void => {
      if (disabled === true) {
        return;
      }

      const imageFiles: File[] = [];
      for (const item of Array.from(event.clipboardData.items)) {
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            imageFiles.push(file);
          }
        }
      }

      if (imageFiles.length === 0) {
        return;
      }

      event.preventDefault();
      for (const file of imageFiles) {
        onImagePaste(file);
      }
    },
    [onImagePaste, disabled],
  );

  return { onPaste };
}
