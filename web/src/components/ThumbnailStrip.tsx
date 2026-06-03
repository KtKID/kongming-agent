import type { JSX } from "react";
import { AlertCircle, Loader2, X } from "lucide-react";

import { cn } from "@/lib/utils";
import type { AttachmentState } from "@/hooks/useAttachmentUploader";

/**
 * 缩略图条：渲染当前 Composer 的附件列表（uploading / ready / failed 三态）。
 *
 * 设计原则：
 * - 纯渲染 + 派发，不持有 state；状态机由 useAttachmentUploader 拥有
 * - 仅消费 local_preview_url（object URL，浏览器内存），不发任何请求
 * - 空列表返回 null（不渲染容器），避免 Composer 出现空高度边框
 *
 * 状态可视化：
 * - uploading：半透明黑层 + 旋转 Loader2
 * - failed：红色边框 + 中间 AlertCircle；hover 显示原生 tooltip (title)
 * - ready：无叠加
 *
 * ✕ 按钮始终显示（含 uploading 阶段，允许取消）。
 */
export interface ThumbnailStripProps {
  attachments: AttachmentState[];
  onRemove: (id: string) => void;
  className?: string;
}

export function ThumbnailStrip({
  attachments,
  onRemove,
  className,
}: ThumbnailStripProps): JSX.Element | null {
  if (attachments.length === 0) {
    return null;
  }

  return (
    <div
      data-testid="attachment-thumbnail-strip"
      className={cn(
        "flex gap-2 overflow-x-auto border-t px-3 py-2",
        className,
      )}
    >
      {attachments.map((a) => (
        <Thumbnail key={a.id} attachment={a} onRemove={onRemove} />
      ))}
    </div>
  );
}

interface ThumbnailProps {
  attachment: AttachmentState;
  onRemove: (id: string) => void;
}

function Thumbnail({ attachment, onRemove }: ThumbnailProps): JSX.Element {
  const { id, status, local_preview_url, filename, error_message } = attachment;

  return (
    <div
      className={cn(
        "relative h-16 w-16 shrink-0 rounded overflow-hidden",
        status === "failed" && "border-2 border-red-500",
      )}
      title={status === "failed" ? error_message : undefined}
    >
      <img
        src={local_preview_url}
        alt={filename}
        className="h-16 w-16 rounded object-cover"
      />

      {status === "uploading" && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50">
          <Loader2 className="h-5 w-5 animate-spin text-white" />
        </div>
      )}

      {status === "failed" && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/40">
          <AlertCircle className="h-5 w-5 text-red-400" />
        </div>
      )}

      <button
        type="button"
        onClick={() => onRemove(id)}
        data-testid={`thumbnail-remove-${id}`}
        aria-label={`移除 ${filename}`}
        className="absolute right-0.5 top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-gray-900/80 text-white hover:bg-gray-900"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}
