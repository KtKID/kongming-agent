/**
 * 内联 diff 视图——以红绿对比展示删除行与新增行。
 * Edit 工具的 arguments 没有真实行号，所以不显示行号。
 */

interface InlineDiffViewProps {
  /** 文件路径，显示在标题栏（取 basename） */
  filePath?: string;
  /** 旧文本（红色删除行） */
  oldText: string;
  /** 新文本（绿色新增行） */
  newText: string;
}

export function InlineDiffView({
  filePath,
  oldText,
  newText,
}: InlineDiffViewProps) {
  const basename = filePath?.split("/").pop();

  const oldLines = oldText ? oldText.split("\n") : [];
  const newLines = newText ? newText.split("\n") : [];

  return (
    <div className="overflow-x-auto rounded border border-border/50 bg-muted/20">
      {/* 标题栏 */}
      {basename && (
        <div className="border-b border-border/30 bg-muted/40 px-2 py-1 text-[11px] font-medium text-muted-foreground">
          {basename}
        </div>
      )}

      <div className="p-1">
        {/* 删除块 */}
        {oldLines.map((line, i) => (
          <div
            key={`old-${i}`}
            className="rounded-sm bg-red-500/10 px-2 font-mono text-[11px] leading-5 text-red-600 dark:text-red-400"
          >
            <span className="select-none">- </span>{line}
          </div>
        ))}

        {/* 新增块 */}
        {newLines.map((line, i) => (
          <div
            key={`new-${i}`}
            className="rounded-sm bg-green-500/10 px-2 font-mono text-[11px] leading-5 text-green-600 dark:text-green-400"
          >
            <span className="select-none">+ </span>{line}
          </div>
        ))}
      </div>
    </div>
  );
}
