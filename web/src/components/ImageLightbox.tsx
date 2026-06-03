import { useEffect, type JSX } from "react";
import { X } from "lucide-react";

/**
 * 图片全屏预览层（lightbox）。
 *
 * 与 MessageList / Composer 解耦：上层通过 `src` 控制开关，`null` 表示关闭。
 * 关闭机制三路：
 *   1. 点击半透明背景（事件 target 是容器本身才关）
 *   2. 点击右上角 ✕ 按钮
 *   3. 按下 ESC 键
 *
 * 纯 div + Tailwind 实现，不引入第三方包；图片用 max-w-[90vw] max-h-[90vh]
 * + object-contain 保证不溢出且保持比例。
 */
export interface ImageLightboxProps {
  src: string | null;
  alt?: string;
  onClose: () => void;
}

export function ImageLightbox({
  src,
  alt,
  onClose,
}: ImageLightboxProps): JSX.Element | null {
  useEffect(() => {
    if (!src) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [src, onClose]);

  if (!src) return null;

  return (
    <div
      data-testid="image-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 p-6"
    >
      <button
        type="button"
        aria-label="关闭预览"
        data-testid="image-lightbox-close"
        onClick={onClose}
        className="absolute right-4 top-4 flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
      >
        <X className="h-5 w-5" />
      </button>
      <img
        src={src}
        alt={alt ?? "preview"}
        className="max-h-[90vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
      />
    </div>
  );
}
