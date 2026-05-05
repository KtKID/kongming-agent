export type CodexPermissionMode = "default" | "acceptEdits" | "bypassPermissions";

const MODES: { value: CodexPermissionMode; label: string; description: string }[] = [
  { value: "acceptEdits", label: "编辑模式", description: "允许修改项目文件，不弹审批" },
  { value: "default", label: "安全模式", description: "危险命令会被沙箱拦截" },
  { value: "bypassPermissions", label: "全权模式", description: "完全放开，包括危险操作" },
];

interface Props {
  value: CodexPermissionMode;
  onChange: (mode: CodexPermissionMode) => void;
  disabled?: boolean;
}

/**
 * Codex 权限模式三档选择器。
 *
 * 项目 UI 无 shadcn Select 组件，使用原生 HTML select + Tailwind 样式。
 * 默认值由上层传入，推荐 `acceptEdits`（避免 default 模式 untrusted 命令卡住）。
 */
export function CodexPermissionModeSelector({ value, onChange, disabled }: Props) {
  return (
    <div className="flex items-center gap-2">
      <label className="text-xs text-muted-foreground whitespace-nowrap" htmlFor="codex-perm-mode">
        权限
      </label>
      <select
        id="codex-perm-mode"
        className="h-8 w-[140px] rounded-md border border-input bg-background px-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        value={value}
        onChange={(e) => onChange(e.target.value as CodexPermissionMode)}
        disabled={disabled}
        title={MODES.find((m) => m.value === value)?.description}
      >
        {MODES.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </select>
    </div>
  );
}
