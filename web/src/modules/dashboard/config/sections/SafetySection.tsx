/**
 * dashboard/config 子模块内部 section，**不要被 sibling 模块直接 import**，
 * 由 ConfigPage 装配后渲染。
 *
 * safety 段一期全量只读 —— 全部字段是 list / dict，不走 FieldRenderer 的标量编辑路径，
 * 直接交给 SafetyRulesView 渲染 8 张 panel + 折叠 yaml。
 *
 * 因此本 section：
 * - **不接受 fields prop**（一期不需要按字段渲染）
 * - **不渲染任何编辑控件**（用户禁止"编辑控件出现在 SafetySection"）
 * - 顶部加固定说明文案
 * - effective 或 raw 未就绪时显示 loading
 *
 * 二期开放 list 字段编辑后，再决定是仍走 SafetyRulesView 内置编辑、还是切换到
 * FieldRenderer 的 list editor —— 当前 section 不为二期做兼容预留。
 */

import { type ReactNode } from "react";

import { SafetyRulesView } from "../components/SafetyRulesView";
import { useConfigStore } from "../store";

export function SafetySection(): ReactNode {
  const effective = useConfigStore((s) => s.effective);
  const raw = useConfigStore((s) => s.raw);

  return (
    <div className="space-y-4" data-section="safety">
      <header className="space-y-1">
        <h3 className="text-base font-semibold text-foreground">安全</h3>
        <p className="text-sm text-muted-foreground">
          安全规则一期全量只读。要修改请直接编辑 setting.yaml 后点重启。
        </p>
      </header>

      {effective && raw ? (
        <SafetyRulesView effective={effective} raw={raw} />
      ) : (
        <p
          className="text-sm text-muted-foreground"
          data-safety-loading="true"
        >
          配置加载中…
        </p>
      )}
    </div>
  );
}
