/**
 * dashboard/config 的全局 safety section。
 *
 * 展示 LLM 审批复核器配置；处置模式由聊天页按 cwd 管理，thread allow/deny
 * 审批本子由 ThreadPermissionsManager 独立管理。
 */

import type { ReactNode } from "react";

import { SafetyRulesView } from "../components/SafetyRulesView";
import type { FieldMeta } from "../types";

export interface SafetySectionProps {
  fields: FieldMeta[];
}

export function SafetySection({ fields }: SafetySectionProps): ReactNode {
  return (
    <div className="space-y-4" data-section="safety">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">安全</h3>
        <p className="text-sm text-muted-foreground">
          配置 LLM 审批复核器。处置模式按项目目录选择；允许与拒绝规则按 thread 保存。
        </p>
      </header>
      <SafetyRulesView fields={fields} />
    </div>
  );
}
