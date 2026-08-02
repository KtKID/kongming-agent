/**
 * dashboard/config 的 safety 配置视图。
 *
 * setting.yaml 只保留 LLM 审批复核器配置；处置模式按 cwd 由聊天页选择器管理，
 * allow/deny 审批本子由 thread permissions 模块按 thread 管理。
 */

import type { ReactNode } from "react";

import { FieldRenderer } from "./FieldRenderer";
import { useConfigStore } from "../store";
import type { FieldMeta } from "../types";

export interface SafetyRulesViewProps {
  fields: FieldMeta[];
}

const GLOBAL_SAFETY_PATHS = new Set([
  "safety.approval.llm",
]);

export function SafetyRulesView({ fields }: SafetyRulesViewProps): ReactNode {
  const effective = useConfigStore((state) => state.effective);
  const dirty = useConfigStore((state) => state.dirty);
  const setField = useConfigStore((state) => state.setField);
  const clearField = useConfigStore((state) => state.clearField);

  if (effective === null) return null;

  const globalFields = fields.filter((meta) => GLOBAL_SAFETY_PATHS.has(meta.path));

  return (
    <div className="space-y-3" data-testid="global-safety-settings">
      {globalFields.map((meta) => {
        const isDirty = meta.path in dirty;
        const value = isDirty ? dirty[meta.path] : effective.values[meta.path];
        return (
          <FieldRenderer
            key={meta.path}
            meta={meta}
            value={value}
            source={effective.sources[meta.path] ?? "default"}
            isDirty={isDirty}
            onChange={(next) => setField(meta.path, next)}
            onClear={() => clearField(meta.path)}
          />
        );
      })}
    </div>
  );
}
