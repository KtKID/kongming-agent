/**
 * smart-approval-v1 前端 feature barrel。
 *
 * 公开 API（外部只能从这里导入）：
 * - `<AutoApprovalModeSelector />`：挂在 Composer 旁的三态选择器
 * - `<CountdownBar />`：挂在审批弹窗里的倒计时条
 * - `useAutoApprovalStore` / `applyStateFrame` / `selectCwdState`：WS handler 用
 * - 类型：AutoApprovalStateFrame / AutoApprovalSocket
 *
 * **不**导出：
 * - queryAutoApproval / setAutoApprovalMode（由组件内部消费 store + socket）
 *   这些是 store API，但允许外部直接调（如 e2e）也可以；保留为 export 方便复用。
 */
export { AutoApprovalModeSelector } from "./AutoApprovalToggle";
export { CountdownBar } from "./CountdownBar";
export {
  type AutoApprovalCwdState,
  type AutoApprovalSocket,
  queryAutoApproval,
  selectCwdState,
  setAutoApprovalMode,
  useAutoApprovalStore,
} from "./useAutoApproval";
export type {
  AutoApprovalQueryFrame,
  AutoApprovalStateFrame,
  AutoApprovalSetModeFrame,
} from "@/protocol";
