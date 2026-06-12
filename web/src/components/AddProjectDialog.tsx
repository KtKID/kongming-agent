import { useEffect, useState } from "react";
import type { JSX } from "react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useThreadsStore } from "@/stores/threads";
import { getServerRepoRoot, isTauriEnv, pickDirectory } from "@/lib/dirPicker";
import { isAbsoluteProjectPath } from "@/lib/path";

export type ProjectKind = "claude" | "codex";

export interface AddProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** claude → 调 addClaudeProject；codex → 调 addCodexProject */
  kind: ProjectKind;
  /** 添加成功后回调，参数为最终落库的 cwd 绝对路径 */
  onAdded?: (cwd: string) => void;
}

/**
 * 双轨目录选择弹窗（v0.1：projects-registry）。
 *
 * 行为分两路：
 *
 * 1. **Tauri 环境**：`open` 切到 true 后立刻调 `pickDirectory()` → 弹原生 OS dialog；
 *    用户选定 → 直接调 `addClaudeProject` / `addCodexProject` → toast + onAdded + 关弹窗。
 *    取消 → 直接关弹窗。**不渲染任何 React UI**（OS dialog 已经是 UI）。
 *
 * 2. **浏览器环境**（`pickDirectory()` 返 null）→ fallback：渲染输入框 + 📍 一键填按钮 +
 *    可选 alias + 提交 / 取消按钮。校验：cwd 必须以 `/` 开头才允许提交。
 *
 * 给 ClaudeProjectsTree / CodexProjectsTree 共用。
 */
export function AddProjectDialog(props: AddProjectDialogProps): JSX.Element {
  const { open, onOpenChange, kind, onAdded } = props;
  const addClaudeProject = useThreadsStore((s) => s.addClaudeProject);
  const addCodexProject = useThreadsStore((s) => s.addCodexProject);

  const [showFallback, setShowFallback] = useState(false);
  const [cwd, setCwd] = useState("");
  const [alias, setAlias] = useState("");
  const [busy, setBusy] = useState(false);
  const [pickerInFlight, setPickerInFlight] = useState(false);

  const title =
    kind === "claude" ? "添加 Claude 项目" : "添加 Codex 项目";

  // 重置内部状态：关闭时清空，打开时让 picker effect 决定要不要 fallback。
  useEffect(() => {
    if (!open) {
      setShowFallback(false);
      setCwd("");
      setAlias("");
      setBusy(false);
      setPickerInFlight(false);
    }
  }, [open]);

  const doAdd = async (targetCwd: string, targetAlias: string) => {
    if (kind === "claude") {
      await addClaudeProject(targetCwd, targetAlias);
    } else {
      await addCodexProject(targetCwd, targetAlias);
    }
  };

  // 打开时立刻尝试 native picker；浏览器返 null 则降级 fallback UI。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPickerInFlight(true);
    void (async () => {
      try {
        const picked = await pickDirectory({ title });
        if (cancelled) return;
        if (picked === null) {
          // Tauri 用户主动取消 → 关弹窗；浏览器环境 → 降级 fallback UI
          if (isTauriEnv()) {
            onOpenChange(false);
          } else {
            setShowFallback(true);
          }
          return;
        }
        // Tauri：原生 dialog 选中，直接 add
        try {
          await doAdd(picked, "");
          toast.success("项目已添加");
          onAdded?.(picked);
          onOpenChange(false);
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err);
          toast.error(`添加失败：${msg}`);
          onOpenChange(false);
        }
      } finally {
        if (!cancelled) setPickerInFlight(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // 仅在 open / kind 变化时重跑；doAdd 闭包 OK，因为这是一次性副作用
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, kind]);

  const normalizedCwd = cwd.trim();
  const cwdValid = isAbsoluteProjectPath(normalizedCwd);
  const canSubmit = !busy && cwdValid && normalizedCwd.length > 0;

  const onPickRepoRoot = async () => {
    try {
      const root = await getServerRepoRoot();
      setCwd(root);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`获取 worktree 失败：${msg}`);
    }
  };

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    try {
      await doAdd(normalizedCwd, alias.trim());
      toast.success("项目已添加");
      onAdded?.(normalizedCwd);
      onOpenChange(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`添加失败：${msg}`);
    } finally {
      setBusy(false);
    }
  };

  // Tauri 路径：只走原生 dialog，不渲染 React UI（仍挂个空 Dialog 占位以便受控状态正常）。
  // 浏览器路径：showFallback === true 才渲染输入 UI。
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {showFallback ? (
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              浏览器环境无法弹原生目录选择器，请输入项目目录的绝对路径，或点击下方按钮自动填入当前 worktree。
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <div className="flex gap-2">
              <Input
                placeholder="E:/proj/my-project"
                value={cwd}
                onChange={(e) => setCwd(e.target.value)}
                aria-label="cwd"
                autoFocus
              />
              <Button
                type="button"
                variant="outline"
                onClick={() => void onPickRepoRoot()}
                title="一键填入当前 server worktree"
              >
                📍
              </Button>
            </div>
            {normalizedCwd.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                绝对路径，例如 E:/xgt/proj/agent-proj/kongming-agent
              </p>
            ) : cwdValid ? (
              <p className="text-xs text-muted-foreground">
                将登记为
                {kind === "claude" ? " Claude " : " Codex "}
                项目入口；之后的 thread 会按此 cwd 关联。
              </p>
            ) : (
              <p className="text-xs text-destructive">
                工作目录需要绝对路径，例如 E:/xgt/proj/agent-proj/kongming-agent
              </p>
            )}
            <Input
              placeholder="项目别名（可选）"
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
              aria-label="alias"
              maxLength={200}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button onClick={() => void onSubmit()} disabled={!canSubmit}>
              添加
            </Button>
          </DialogFooter>
        </DialogContent>
      ) : (
        // Tauri 路径或 picker 在飞 → 渲染极简占位（不弹 UI），让 OS dialog 唱主角。
        // DialogContent 必须存在以保证 Radix Dialog 受控行为正常。
        <DialogContent className="sr-only" aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
          </DialogHeader>
          <p className="text-xs text-muted-foreground">
            {pickerInFlight ? "正在打开目录选择器…" : ""}
          </p>
        </DialogContent>
      )}
    </Dialog>
  );
}
