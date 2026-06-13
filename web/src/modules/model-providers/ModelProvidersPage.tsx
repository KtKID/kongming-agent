import { useEffect, type ReactNode } from "react";
import { Edit3, Link2, RefreshCw, TestTube2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

import { useModelProvidersStore } from "./store";
import type { ProviderListItem } from "./types";

const API_KEY_MIN_LENGTH = 8;

function providerTitle(item: ProviderListItem): string {
  return `${item.displayName}（${item.regionLabel}）`;
}

function ProviderStatus({ status }: { status: ProviderListItem["status"] }): ReactNode {
  const connected = status === "connected";
  const errored = status === "error";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        connected && "bg-success/12 text-success",
        errored && "bg-destructive/10 text-destructive",
        !connected && !errored && "bg-muted text-muted-foreground",
      )}
      data-testid="provider-status"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {connected ? "已连接" : errored ? "异常" : "未连接"}
    </span>
  );
}

function ProviderRow({ item }: { item: ProviderListItem }): ReactNode {
  const openDialog = useModelProvidersStore((s) => s.openDialog);
  const testCurrent = useModelProvidersStore((s) => s.testCurrent);
  const currentTestStatus = useModelProvidersStore(
    (s) => s.ui.currentTestStatus[item.providerId] ?? "idle",
  );
  const feedback = useModelProvidersStore((s) => s.ui.feedback[item.providerId]);
  const connected = item.status === "connected";
  const title = providerTitle(item);

  return (
    <article
      className="grid min-h-[76px] grid-cols-[44px_minmax(0,1fr)] items-center gap-3 px-3 py-3 sm:grid-cols-[44px_minmax(0,1fr)_auto] sm:px-4"
      data-testid={`provider-row-${item.providerId}`}
    >
      <div
        className="grid h-11 w-11 place-items-center rounded-xl border border-primary/20 bg-secondary/70 text-base font-bold text-accent"
        aria-hidden="true"
      >
        {item.logoText}
      </div>

      <div className="min-w-0">
        <h3 className="flex flex-wrap items-center gap-2 text-base font-semibold tracking-normal text-foreground">
          {title}
          <ProviderStatus status={item.status} />
        </h3>
        <p className="mt-1 text-sm leading-5 text-muted-foreground">
          {item.description}
        </p>
        <p
          className={cn(
            "mt-1 min-h-4 text-xs text-muted-foreground",
            currentTestStatus === "success" && "text-success",
            currentTestStatus === "error" && "text-destructive",
          )}
          aria-live="polite"
        >
          {feedback}
        </p>
      </div>

      <div className="col-span-2 grid grid-cols-2 gap-2 sm:col-span-1 sm:flex sm:items-center sm:justify-end">
        {connected ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void testCurrent(item.providerId)}
            disabled={currentTestStatus === "running"}
            data-testid="provider-current-test"
          >
            <TestTube2 />
            {currentTestStatus === "running" ? "测试中" : "测试"}
          </Button>
        ) : null}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => openDialog(item.providerId)}
          className={cn(!connected && "col-span-2 sm:col-span-1")}
        >
          {connected ? <Edit3 /> : <Link2 />}
          {connected ? "编辑" : "连接"}
        </Button>
      </div>
    </article>
  );
}

function ConnectDialog(): ReactNode {
  const items = useModelProvidersStore((s) => s.items);
  const dialog = useModelProvidersStore((s) => s.dialog);
  const closeDialog = useModelProvidersStore((s) => s.closeDialog);
  const setDraftApiKey = useModelProvidersStore((s) => s.setDraftApiKey);
  const reloadDialogConnection = useModelProvidersStore((s) => s.reloadDialogConnection);
  const testDraft = useModelProvidersStore((s) => s.testDraft);
  const saveConnection = useModelProvidersStore((s) => s.saveConnection);

  const provider = items.find((item) => item.providerId === dialog.providerId);
  const apiKey = dialog.draftApiKey.trim();
  const keyReady = apiKey.length >= API_KEY_MIN_LENGTH;
  const saveReady =
    keyReady &&
    dialog.testedApiKey === apiKey &&
    dialog.testStatus === "success" &&
    dialog.saveStatus !== "running";
  const open = Boolean(provider);
  const title = provider ? providerTitle(provider) : "";
  const connected = provider?.status === "connected";
  const reloading = dialog.reloadStatus === "running";

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) closeDialog();
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {connected ? "编辑" : "连接"} {title}
          </DialogTitle>
          <DialogDescription>API Key 将写入本地配置。</DialogDescription>
        </DialogHeader>

        <div className="grid gap-2">
          <label className="text-sm font-medium text-foreground" htmlFor="provider-api-key">
            API Key
          </label>
          <Input
            id="provider-api-key"
            type="password"
            autoComplete="off"
            value={dialog.draftApiKey}
            placeholder={`输入 ${provider?.displayName ?? "服务商"} API Key`}
            onChange={(event) => setDraftApiKey(event.target.value)}
          />
          <p
            className={cn(
              "min-h-5 text-sm text-muted-foreground",
              (dialog.testStatus === "success" || dialog.reloadStatus === "success") &&
                "text-success",
              (dialog.testStatus === "error" ||
                dialog.saveStatus === "error" ||
                dialog.reloadStatus === "error") &&
                "text-destructive",
            )}
            aria-live="polite"
            data-testid="provider-dialog-message"
          >
            {dialog.message}
          </p>
        </div>

        <DialogFooter className="gap-2 sm:space-x-0">
          <Button type="button" variant="ghost" onClick={closeDialog}>
            取消
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void reloadDialogConnection()}
            disabled={reloading}
          >
            <RefreshCw className={cn(reloading && "animate-spin")} />
            {reloading ? "读取中" : "重新读取"}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => void testDraft()}
            disabled={!keyReady || dialog.testStatus === "running"}
          >
            <TestTube2 />
            {dialog.testStatus === "running" ? "测试中" : "测试"}
          </Button>
          <Button
            type="button"
            onClick={() => void saveConnection()}
            disabled={!saveReady}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ProviderGroup({
  title,
  items,
  emptyText,
}: {
  title: string;
  items: ProviderListItem[];
  emptyText: string;
}): ReactNode {
  return (
    <section className="grid gap-2" aria-label={title}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-foreground">{title}</h3>
        <span className="text-xs text-muted-foreground">{items.length}</span>
      </div>
      {items.length > 0 ? (
        <div className="divide-y divide-border/70 overflow-hidden rounded-xl border border-border/80 bg-card/72 shadow-sm">
          {items.map((item) => (
            <ProviderRow key={item.providerId} item={item} />
          ))}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-border/80 px-4 py-3 text-sm text-muted-foreground">
          {emptyText}
        </div>
      )}
    </section>
  );
}

export function ModelProvidersPage(): ReactNode {
  const items = useModelProvidersStore((s) => s.items);
  const loadStatus = useModelProvidersStore((s) => s.loadStatus);
  const loadError = useModelProvidersStore((s) => s.loadError);
  const load = useModelProvidersStore((s) => s.load);

  useEffect(() => {
    if (loadStatus === "idle") {
      void load();
    }
  }, [load, loadStatus]);

  if (loadStatus === "loading") {
    return (
      <div className="p-6 text-sm text-muted-foreground" data-testid="model-providers-loading">
        加载模型服务商中...
      </div>
    );
  }

  if (loadStatus === "error") {
    return (
      <div className="p-6 text-sm text-destructive" data-testid="model-providers-error">
        加载失败：{loadError ?? "未知错误"}
      </div>
    );
  }

  const connectedItems = items.filter((item) => item.status === "connected");
  const disconnectedItems = items.filter((item) => item.status !== "connected");

  return (
    <section className="min-w-0" data-testid="model-providers-page">
      <div className="mb-4">
        <h2 className="text-base font-semibold tracking-tight text-foreground">
          模型服务商
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          管理 Web 侧可用的模型服务商连接。
        </p>
      </div>

      <div className="grid gap-5">
        <ProviderGroup
          title="已连接"
          items={connectedItems}
          emptyText="暂无已连接的模型服务商。"
        />
        <ProviderGroup
          title="未连接"
          items={disconnectedItems}
          emptyText="暂无未连接的模型服务商。"
        />
      </div>

      <ConnectDialog />
    </section>
  );
}
