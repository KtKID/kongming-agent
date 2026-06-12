/**
 * dashboard/config 子模块内部 section，**不要被 sibling 模块直接 import**，
 * 由 ConfigPage 装配后渲染。
 *
 * 渲染 model 组所有字段：
 * - 通过 props 接收已经按 group=model 过滤好的 FieldMeta[]，本组件不再做二次过滤
 *   （过滤职责在 ConfigPage，避免每个 section 各做一遍）
 * - 从 useConfigStore 取 effective / dirty 拉值，调 setField / clearField 写回脏字段
 * - 底部追加 YamlPreview 展示 `setting.yaml` 中 `model:` 段原文，方便核对
 *   `reasoning_profiles` 等列表字段的写盘结构
 *
 * 不在本文件 fetch / 修改 store 全局状态（除调用 setField / clearField）。
 */

import { type ReactNode } from "react";

import { FieldRenderer } from "../components/FieldRenderer";
import { YamlPreview } from "../components/YamlPreview";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

import { extractTopLevelSection } from "./_yaml";

export interface ModelSectionProps {
  /** 已过滤为 group=model 的字段，按 schema 原始顺序传入。 */
  fields: FieldMeta[];
}

export function ModelSection({ fields }: ModelSectionProps): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const dirty = useConfigStore((s) => s.dirty);
  const raw = useConfigStore((s) => s.raw);
  const setField = useConfigStore((s) => s.setField);
  const clearField = useConfigStore((s) => s.clearField);

  if (!effective) return null;

  return (
    <div className="space-y-4" data-section="model">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">模型</h3>
        <p className="text-sm text-muted-foreground">
          主 agent 调用 LLM 的所有参数：模型名、生成温度、重试 / 流式策略、推理强度档位等。
        </p>
      </header>

      <div className="space-y-3">
        {fields.map((meta) => {
          const value =
            meta.path in dirty ? dirty[meta.path] : effective.values[meta.path];
          const source = effective.sources[meta.path] ?? "default";
          const isDirty = meta.path in dirty;
          return (
            <FieldRenderer
              key={meta.path}
              meta={meta}
              value={value}
              source={source}
              isDirty={isDirty}
              onChange={(v) => setField(meta.path, v)}
              onClear={() => clearField(meta.path)}
            />
          );
        })}
      </div>

      {raw ? (
        <YamlPreview
          content={extractTopLevelSection(raw.content, "model")}
          title="model 段 yaml 原文"
        />
      ) : null}
    </div>
  );
}
