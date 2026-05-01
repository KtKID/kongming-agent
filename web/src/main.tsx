import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { toast } from "sonner";
import App from "./App";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Toaster } from "@/components/ui/sonner";
import "./styles/globals.css";

// 全局 unhandled error / rejection → toast，避免静默吞错
window.addEventListener("error", (e) => {
  toast.error(`运行错误：${e.message}`);
});
window.addEventListener("unhandledrejection", (e) => {
  const reason = (e.reason as Error | string | undefined) ?? "unknown";
  const msg = reason instanceof Error ? reason.message : String(reason);
  toast.error(`未处理异常：${msg}`);
});

const root = document.getElementById("root");
if (!root) throw new Error("#root not found in index.html");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
      <Toaster richColors position="top-center" />
    </ErrorBoundary>
  </StrictMode>,
);
