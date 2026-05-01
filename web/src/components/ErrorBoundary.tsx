import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 根级 ErrorBoundary：兜底任何 React 渲染异常。
 * v0.1.5 简版：固定 fallback；如需精细化错误页可注入 fallback prop。
 */
interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // dev 控制台留个 trace；上线后可接 sentry/log endpoint
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  override render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-background p-8 text-foreground">
          <div className="text-2xl font-semibold">出错了</div>
          <div className="max-w-md text-center text-sm text-muted-foreground">
            {this.state.error?.message ?? "未知错误"}
          </div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
          >
            刷新页面
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
