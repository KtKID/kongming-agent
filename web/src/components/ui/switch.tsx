import * as React from "react";

/**
 * 轻量 Switch（不依赖 @radix-ui/react-switch，避免引入新 deps）。
 *
 * 接口与 shadcn ui Switch 兼容：
 * - `checked` 受控值
 * - `onCheckedChange(next)` 受控回调
 * - `disabled` / `aria-label` 透传
 */
interface SwitchProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> {
  checked: boolean;
  onCheckedChange?: (next: boolean) => void;
}

export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  (
    { checked, onCheckedChange, disabled, className, ...rest },
    ref,
  ) => {
    return (
      <button
        ref={ref}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={(e) => {
          if (disabled) return;
          onCheckedChange?.(!checked);
          rest.onClick?.(e);
        }}
        className={[
          "inline-flex h-4 w-7 shrink-0 cursor-pointer items-center rounded-full border border-border/60",
          "transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          "disabled:cursor-not-allowed disabled:opacity-50",
          checked ? "bg-primary" : "bg-input",
          className ?? "",
        ].join(" ")}
        {...rest}
      >
        <span
          className={[
            "pointer-events-none block h-3 w-3 rounded-full bg-background shadow",
            "transition-transform",
            checked ? "translate-x-3.5" : "translate-x-0.5",
          ].join(" ")}
          aria-hidden="true"
        />
      </button>
    );
  },
);
Switch.displayName = "Switch";
