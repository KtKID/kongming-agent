/**
 * smart-approval-v2-inbox 前端 feature barrel。
 *
 * 公开 API：
 * - ``<ApprovalToastQueue />``：右下角全局浮窗（挂在 Layout.tsx 上）
 * - ``<ApprovalToastCard />``：单卡片（一般由 Queue 内部使用，外部很少直接用）
 * - ``useApprovalInboxStore`` / ``selectSorted``：store + 排序 selector
 * - 协议类型：``ApprovalInbox*Frame`` / ``ApprovalInboxItem``
 *
 * 上层 hook（``useThreadStatusWS``）职责（#8 任务）：
 * - 解析 ``approval.inbox.add|remove|snapshot`` 帧 → 调对应 ``apply*Frame``
 * - 注入 send 通道 → ``setSender(frame => ws.send(JSON.stringify(frame)))``（**走 module-level senderRef，不进 store**——避免 React 19 useSyncExternalStore tearing → #185 / ws 重连风暴）
 */
export { ApprovalToastCard } from "./ApprovalToastCard";
export { ApprovalToastQueue } from "./ApprovalToastQueue";
export {
  selectSorted,
  useApprovalInboxStore,
} from "./useApprovalInbox";
export { setSender, getSender, resetSender } from "./senderRef";
export type {
  ApprovalInboxAddFrame,
  ApprovalInboxItem,
  ApprovalInboxRemoveFrame,
  ApprovalInboxResolveFrame,
  ApprovalInboxSnapshotFrame,
} from "./types";
