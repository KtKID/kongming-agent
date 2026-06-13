import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
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
import type { BackendKind } from "@/protocol";
import { isAbsoluteProjectPath } from "@/lib/path";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * 新建 thread dialog：backend + name + (preset) 选择 → POST /api/threads → 跳转 /chat/<id>
 *
 * v0.1.6：加 backend 选择。`generic_chat` 走原 LLM provider 路径，需要 preset；
 * `claude_code` 走 Claude Agent SDK，preset 不适用（隐藏）。
 */
export function NewThreadDialog({ open, onOpenChange }: Props) {
  const presets = useThreadsStore((s) => s.presets);
  const createThread = useThreadsStore((s) => s.createThread);
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [backendKind, setBackendKind] = useState<BackendKind>("generic_chat");
  const [presetId, setPresetId] = useState("");
  const [cwd, setCwd] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setBackendKind("generic_chat");
      setPresetId(presets[0]?.id ?? "");
      setCwd("");
    }
  }, [open, presets]);

  const isClaudeCode = backendKind === "claude_code";
  const isCodex = backendKind === "codex";
  const noPresetNeeded = isClaudeCode || isCodex;
  const normalizedCwd = cwd.trim();
  const cwdValid = !normalizedCwd || isAbsoluteProjectPath(normalizedCwd);
  const canSubmit =
    !busy && cwdValid && (noPresetNeeded || !!presetId);

  const onSubmit = async () => {
    const trimmed = name.trim();
    if (!canSubmit) return;
    setBusy(true);
    try {
      const t = await createThread(
        trimmed,
        noPresetNeeded ? "" : presetId,
        backendKind,
        normalizedCwd,
      );
      onOpenChange(false);
      navigate(`/chat/${t.id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`创建失败：${msg}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建会话</DialogTitle>
          <DialogDescription>
            选择后端类型并命名一个新会话
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Input
            placeholder="不填则使用服务器ID"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
            maxLength={200}
          />
          <Input
            placeholder="工作目录（可选，绝对路径）"
            value={cwd}
            onChange={(e) => setCwd(e.target.value)}
            aria-label="cwd"
          />
          {cwdValid ? (
            <p className="text-xs text-muted-foreground">
              填写后 Files / Shell 会直接绑定这个 workspace；留空保持纯聊天。
            </p>
          ) : (
            <p className="text-xs text-destructive">
              工作目录需要绝对路径，例如 E:/xgt/proj/agent-proj/kongming-agent
            </p>
          )}
          <select
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={backendKind}
            onChange={(e) => setBackendKind(e.target.value as BackendKind)}
            aria-label="backend"
          >
            <option value="generic_chat">通用对话（preset）</option>
            <option value="claude_code">Claude Code</option>
            <option value="codex">Codex</option>
          </select>
          {noPresetNeeded ? null : (
            <select
              className="h-9 rounded-md border border-input bg-background px-3 text-sm"
              value={presetId}
              onChange={(e) => setPresetId(e.target.value)}
              aria-label="preset"
            >
              {presets.length === 0 ? (
                <option value="">（暂无 preset）</option>
              ) : null}
              {presets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name} · {p.model}
                </option>
              ))}
            </select>
          )}
          {isClaudeCode ? (
            <p className="text-xs text-muted-foreground">
              Claude Code 路径走 Claude Agent SDK，无需 preset；刷新页面会丢失对话历史（v0.1）。
            </p>
          ) : isCodex ? (
            <p className="text-xs text-muted-foreground">
              Codex 路径走 codex CLI 子进程，无需 preset。
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={() => void onSubmit()} disabled={!canSubmit}>
            创建
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
