import { useEffect, useId, useRef, useState } from "react";
import mermaid from "mermaid";

/**
 * Mermaid 流程图块。
 *
 * 流式兼容：mermaid 源在 LLM 流式输出过程中可能语法不完整，
 * render() reject 时静默降级为 `<pre>` 原码，等下次 source 变化重试。
 *
 * 唯一 id：useId() + 计数避免 mermaid 内部 DOM 冲突（mermaid 会用 id 创建临时 <g>）。
 *
 * 安全：SVG 由 mermaid 本身生成（受信任输出），不是用户的 HTML，
 * 用 dangerouslySetInnerHTML 注入是安全的；mermaid 已开 securityLevel='strict'
 * 阻止源码内的 `<script>` 注入。
 */

let initialized = false;
function ensureInit() {
  if (initialized) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: "default",
    securityLevel: "strict",
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", system-ui, sans-serif',
  });
  initialized = true;
}

interface MermaidBlockProps {
  source: string;
}

export default function MermaidBlock({ source }: MermaidBlockProps) {
  const reactId = useId();
  // mermaid 要求 id 是合法 CSS 选择器，去掉 useId 的 `:`
  const idRef = useRef(`mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`);
  const [svg, setSvg] = useState<string | null>(null);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    ensureInit();
    let cancelled = false;
    setErrored(false);

    mermaid
      .render(idRef.current, source)
      .then((result: { svg: string }) => {
        if (!cancelled) setSvg(result.svg);
      })
      .catch(() => {
        // 流式中间态或语法错误：静默降级，不刷屏 console
        if (!cancelled) {
          setSvg(null);
          setErrored(true);
        }
      });

    return () => {
      cancelled = true;
      // 清理 mermaid 注入到 body 的临时元素（用 id 兜底）
      const tmp = document.getElementById(idRef.current);
      if (tmp && tmp.parentElement === document.body) {
        tmp.remove();
      }
    };
  }, [source]);

  if (svg) {
    return (
      <div
        className="my-2 overflow-x-auto"
        // SVG 是 mermaid 自己生成的受信输出
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    );
  }

  // 兜底：渲染中或失败时显示原码
  return (
    <pre
      className={
        "my-2 overflow-x-auto rounded-md border border-border bg-muted p-3 text-xs" +
        (errored ? " border-destructive/30" : "")
      }
    >
      <code className="font-mono">{source}</code>
    </pre>
  );
}
