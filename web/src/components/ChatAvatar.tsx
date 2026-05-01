/**
 * 聊天气泡头像：内联 SVG，零外部依赖。
 *
 * - user：右侧，人形占位符
 * - assistant：左侧，机器人图标
 * - tool / approval / error：不显示头像（由调用方决定）
 */

interface ChatAvatarProps {
  role: "user" | "assistant";
  className?: string;
}

export function ChatAvatar({ role, className = "" }: ChatAvatarProps) {
  const base =
    "flex h-8 w-8 shrink-0 items-center justify-center rounded-full select-none";

  if (role === "user") {
    return (
      <div className={`${base} bg-accent text-accent-foreground ${className}`}>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-4 w-4"
        >
          <circle cx="12" cy="8" r="4" />
          <path d="M20 21a8 8 0 1 0-16 0" />
        </svg>
      </div>
    );
  }

  // assistant
  return (
    <div className={`${base} bg-primary text-primary-foreground ${className}`}>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-4 w-4"
      >
        <path d="M12 2a4 4 0 0 1 4 4v1a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z" />
        <rect x="6" y="9" width="12" height="10" rx="2" />
        <circle cx="9.5" cy="14" r="1" fill="currentColor" stroke="none" />
        <circle cx="14.5" cy="14" r="1" fill="currentColor" stroke="none" />
        <path d="M9.5 17h5" />
      </svg>
    </div>
  );
}
