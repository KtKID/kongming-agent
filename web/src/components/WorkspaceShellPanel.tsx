import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, FileTerminal, Square, Terminal } from "lucide-react";
import { useWorkspaceShellWS } from "@/hooks/useWorkspaceShellWS";
import type { WorkspaceContextDTO, WorkspaceShellS2CFrame } from "@/protocol";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface WorkspaceShellPanelProps {
  context?: WorkspaceContextDTO;
  loading?: boolean;
}

export function WorkspaceShellPanel({
  context,
  loading = false,
}: WorkspaceShellPanelProps) {
  const shellEnabled = Boolean(context?.shell_available && context?.thread_id);
  const { socket, state } = useWorkspaceShellWS(
    shellEnabled ? context?.thread_id : undefined,
  );
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");
  const [lastStatus, setLastStatus] = useState<Extract<
    WorkspaceShellS2CFrame,
    { type: "shell-status" }
  > | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setOutput("");
    setInput("");
    setLastStatus(null);
  }, [context?.thread_id]);

  useEffect(() => {
    if (!socket) return;
    const off = socket.on((frame) => {
      if (frame.type === "shell-output") {
        setOutput((prev) => `${prev}${frame.data}`);
        return;
      }
      if (frame.type === "shell-status") {
        setLastStatus(frame);
        const line =
          frame.status === "starting"
            ? `$ ${frame.command.join(" ")}\n`
            : frame.status === "exited"
              ? `\n[process exited: ${frame.exitCode ?? 0}]\n`
              : frame.status === "terminated"
                ? "\n[process terminated]\n"
                : "";
        if (line) setOutput((prev) => `${prev}${line}`);
        return;
      }
      if (frame.type === "shell-error") {
        setOutput((prev) => `${prev}\n[shell error] ${frame.detail}\n`);
      }
    });
    return () => {
      off();
    };
  }, [socket]);

  useEffect(() => {
    if (!socket || state !== "open") return;
    socket.send({ type: "shell-resize", cols: 120, rows: 32 });
  }, [socket, state]);

  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [output]);

  const commandLabel = useMemo(() => {
    if (lastStatus?.command?.length) return lastStatus.command.join(" ");
    if (context?.shell_provider === "system_shell") {
      return "workspace shell";
    }
    if (!context?.claude_thread_id) return "claude";
    return `claude --resume ${context.claude_thread_id}`;
  }, [context?.claude_thread_id, context?.shell_provider, lastStatus?.command]);

  const providerLabel = useMemo(() => {
    if (lastStatus?.command?.length) {
      return lastStatus.command[0]?.includes("claude") ? "claude_code" : "system_shell";
    }
    return context?.shell_provider ?? "none";
  }, [context?.shell_provider, lastStatus?.command]);

  function sendLine(): void {
    if (!socket || !input.trim()) return;
    const line = input;
    setOutput((prev) => `${prev}> ${line}\n`);
    socket.send({ type: "shell-input", data: `${line}\n` });
    setInput("");
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        正在加载 shell 上下文...
      </div>
    );
  }
  if (!context) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        先选择一个 thread
      </div>
    );
  }
  if (!context.shell_available) {
    return (
          <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-3xl border border-border bg-card p-6 text-center">
          <div className="text-lg font-semibold">Workspace Shell 当前不可用</div>
          <p className="mt-2 text-sm text-muted-foreground">
            {context.unavailable_reason ?? "当前 thread 还没有可用的 workspace shell 上下文"}
          </p>
        </div>
      </div>
    );
  }
  return (
    <div
      data-testid="workspace-shell-panel"
      className="flex h-full min-h-0 flex-col overflow-hidden"
    >
      <div className="border-b border-border px-5 py-4">
        <div className="flex items-center gap-2 text-sm font-medium text-foreground">
          <FileTerminal className="h-4 w-4" />
          Workspace Shell
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-3 py-1 text-xs text-foreground">
            {providerLabel === "claude_code" ? (
              <Bot className="h-3 w-3" />
            ) : (
              <Terminal className="h-3 w-3" />
            )}
            {providerLabel}
          </span>
          <span className="truncate text-sm text-muted-foreground">
            {context.workspace_root}
          </span>
        </div>
        <div className="mt-2 text-xs text-muted-foreground">
          命令：{commandLabel}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden p-4">
        <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-[1.5rem] border border-border bg-zinc-950 text-zinc-100">
          <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3 text-xs text-zinc-400">
            <span>socket: {state}</span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => socket?.send({ type: "shell-terminate" })}
              className="h-8 gap-1.5 text-zinc-200 hover:bg-zinc-900 hover:text-zinc-50"
            >
              <Square className="h-3.5 w-3.5" />
              停止
            </Button>
          </div>
          <div
            ref={viewportRef}
            className="min-h-0 flex-1 overflow-auto px-4 py-4 font-mono text-[13px] leading-6"
          >
            <pre className="whitespace-pre-wrap break-words">{output || "正在启动 Claude shell...\n"}</pre>
          </div>
          <div className="border-t border-zinc-800 px-4 py-3">
            <div className="flex gap-2">
              <Input
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    sendLine();
                  }
                }}
                placeholder="输入一行命令；如果当前是 Claude shell，也可以直接发回复"
                className="border-zinc-700 bg-zinc-900 text-zinc-50 placeholder:text-zinc-500"
              />
              <Button type="button" size="sm" onClick={sendLine} disabled={!input.trim()}>
                发送
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
