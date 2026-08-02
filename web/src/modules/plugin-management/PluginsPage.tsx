import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AlertCircle, PackageCheck, Plug, RefreshCw } from "lucide-react";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type { PluginToolDTO } from "@/protocol";

import { listPluginTools, updatePluginTool } from "./api";

export function PluginsPage(): ReactNode {
  const [plugins, setPlugins] = useState<PluginToolDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingById, setPendingById] = useState<Record<string, boolean>>({});

  const loadPlugins = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listPluginTools();
      setPlugins(response.plugins);
    } catch (err) {
      setError(formatPluginError(err, "读取插件工具失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPlugins();
  }, [loadPlugins]);

  const handleEnabledChange = async (plugin: PluginToolDTO, enabled: boolean) => {
    setError(null);
    setPendingById((current) => ({ ...current, [plugin.id]: true }));
    setPlugins((current) =>
      current.map((item) => (item.id === plugin.id ? { ...item, enabled } : item)),
    );
    try {
      const updated = await updatePluginTool(plugin.id, { enabled });
      setPlugins((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch (err) {
      setPlugins((current) =>
        current.map((item) => (item.id === plugin.id ? plugin : item)),
      );
      setError(formatPluginError(err, "更新插件开关失败"));
    } finally {
      setPendingById((current) => {
        const next = { ...current };
        delete next[plugin.id];
        return next;
      });
    }
  };

  return (
    <section className="min-w-0 rounded-2xl border border-border/70 bg-card/74 shadow-sm">
      <div className="border-b border-border/70 px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold tracking-tight text-foreground">
              插件
            </h3>
          </div>
          <div
            className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-primary/20 bg-primary/10 text-primary"
            aria-hidden="true"
          >
            <Plug className="h-4 w-4" />
          </div>
        </div>
      </div>

      <div className="px-5 py-5">
        <section aria-labelledby="registered-plugins-title">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <PackageCheck className="h-4 w-4 text-muted-foreground" />
              <h4
                id="registered-plugins-title"
                className="text-sm font-semibold text-foreground"
              >
                已注册插件
              </h4>
            </div>
            <button
              type="button"
              onClick={() => void loadPlugins()}
              disabled={loading}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border/70 bg-background/60 px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-55"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading ? "animate-spin" : "")} />
              刷新
            </button>
          </div>

          {error ? (
            <div
              role="alert"
              className="mb-3 flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              <AlertCircle className="h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          ) : null}

          <Separator className="mb-3 bg-border/70" />

          {loading ? (
            <div
              data-testid="plugin-management-loading"
              className="flex min-h-[96px] items-center justify-center rounded-xl border border-border/70 bg-background/40 text-sm text-muted-foreground"
            >
              正在读取已注册工具
            </div>
          ) : plugins.length === 0 ? (
            <div className="flex min-h-[96px] items-center justify-center rounded-xl border border-border/70 bg-background/40 text-sm text-muted-foreground">
              当前没有已注册插件工具
            </div>
          ) : (
            <div className="divide-y divide-border/60 overflow-hidden rounded-xl border border-border/70">
              {plugins.map((plugin) => (
                <PluginRow
                  key={plugin.id}
                  plugin={plugin}
                  pending={pendingById[plugin.id] ?? false}
                  onEnabledChange={(next) => void handleEnabledChange(plugin, next)}
                />
              ))}
            </div>
          )}
        </section>
      </div>
    </section>
  );
}

function PluginRow({
  plugin,
  pending,
  onEnabledChange,
}: {
  plugin: PluginToolDTO;
  pending: boolean;
  onEnabledChange: (next: boolean) => void;
}): ReactNode {
  const subtitle = [plugin.server_id, plugin.mcp_tool_name].filter(Boolean).join(" / ");

  return (
    <article className="grid min-h-[72px] grid-cols-[42px_minmax(0,1fr)_auto] items-center gap-3 bg-background/28 px-3 py-3">
      <div
        className="grid h-10 w-10 place-items-center rounded-lg border border-blue-500/24 bg-blue-500/10 text-blue-500"
        aria-hidden="true"
      >
        <Plug className="h-5 w-5" />
      </div>

      <div className="min-w-0">
        <h5 className="truncate text-sm font-semibold text-foreground">
          {plugin.display_name}
        </h5>
        {plugin.description ? (
          <p className="mt-1 truncate text-xs text-muted-foreground">
            {plugin.description}
          </p>
        ) : null}
        <p className="mt-1 truncate text-[11px] text-muted-foreground/80">
          {subtitle}
        </p>
      </div>

      <Switch
        checked={plugin.enabled}
        disabled={pending}
        onCheckedChange={onEnabledChange}
        aria-label={`${plugin.display_name} 插件开关`}
      />
    </article>
  );
}

function formatPluginError(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) {
    return err.message;
  }
  return fallback;
}
