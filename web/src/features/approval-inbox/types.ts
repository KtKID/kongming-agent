/**
 * smart-approval-v2-inbox 协议类型（前端 ↔ 后端对齐）。
 *
 * 后端真源：
 * - 帧 fan-out：``src/web/global_approvals/broadcaster.py::ApprovalInboxBroadcaster``
 *   - ``emit_add`` → ``approval.inbox.add``
 *   - ``emit_remove`` → ``approval.inbox.remove``
 *   - ``push_snapshot`` → ``approval.inbox.snapshot``（重连补包）
 * - resolve 路由：``broadcaster.resolve(thread_id, request_id, decision)``
 *
 * **设计要点**：
 * - inbox 是跨 thread 的全局视图；resolve 帧必须带 ``threadId`` 让后端按 bridge_registry 路由。
 * - autoApproveAtMs / autoRejectAtMs 互斥；elevated 时两者都为 null（必须人审）。
 * - blockedByRule 非空 = 危险卡（顶置 + 红边框）。
 */

/** 单条 inbox 审批的 payload（不含 kind；snapshot.items 的元素就是这个形状） */
export interface ApprovalInboxItem {
  requestId: string;
  threadId: string;
  toolName: string;
  toolInput: unknown;
  /** v1：绝对时间戳 ms；非空 = 安全路径倒计时自动**通过**；空 = 必须人审 */
  autoApproveAtMs: number | null;
  /** v1.0.1：绝对时间戳 ms；非空 = 危险路径倒计时自动**拒绝**（fail-closed） */
  autoRejectAtMs: number | null;
  /** 命中的危险规则 id；空 = 未命中（普通卡） */
  blockedByRule: string | null;
  /** elevated 模式 = 高敏 capability（无倒计时） */
  isElevated: boolean;
  /** 通道标识（claude_code / generic_chat / ...） */
  channel: string;
  /** thread 的 cwd（用于显示） */
  cwd: string;
  /** 后端写入 snapshot 的到达时间（用于排序） */
  arrivedAtMs: number;
  /**
   * v0.5（smart-approval-generic-chat-autoallow task #1）：
   * 后端 ``_PendingApproval.timeout_ms`` 透传（毫秒）。
   *
   * 用途：前端兜底倒计时——当 ``autoApproveAtMs`` / ``autoRejectAtMs`` 都为 null
   * 时，用 ``arrivedAtMs + timeoutMs`` 推一个 **reject** 倒计时（与后端 60s
   * timeout fail-closed 行为对齐，**不能误导用户以为会 approve**）。
   *
   * 老后端兼容：未注入字段时为 ``null``（v2-inbox 老帧不带 timeoutMs，
   * 无 fallback 倒计时；保持原行为不变）。
   */
  timeoutMs: number | null;
}

// ----- S2C 协议帧（前端 onmessage 接收） -----

/** 新审批到达 */
export interface ApprovalInboxAddFrame extends ApprovalInboxItem {
  frame_type: "approval.inbox.add";
}

/** 审批结束（用户操作 / 超时 / 取消） */
export interface ApprovalInboxRemoveFrame {
  frame_type: "approval.inbox.remove";
  requestId: string;
  reason: "user_decided" | "timeout" | "cancelled";
}

/** 连接建立时的全量补包（重连补缺） */
export interface ApprovalInboxSnapshotFrame {
  frame_type: "approval.inbox.snapshot";
  items: ApprovalInboxItem[];
}

// ----- C2S 协议帧（前端发出） -----

/**
 * 用户决定。``threadId`` 必填——后端按 threadId 查 bridge_registry，
 * 缺失 → silent continue（drop 此帧）。
 *
 * ``rememberScope``（generic-chat-session-grant fix）：
 * - ``"session"``：让 ``ApprovalManager.resolve`` 触发
 *   ``ApprovalRules.add_session_grant``——thread 级写入
 *   ``_thread_overrides``，下次同 (cwd, tool_name) 跳过弹卡 immediate allow
 * - ``"persistent"``：v0.5 协议演进留位（rules.yaml 持久化）
 * - 不传：单次同意，不写 session 级
 *
 * claude_code v2-inbox 老路径走 ``ApprovalBridge.resolve``，依据
 * ``rememberEntry`` 字段触发老 grant store；本字段在该路径下被忽略
 * （后端做兼容路由，前端可放心两字段都带）。
 */
export interface ApprovalInboxResolveFrame {
  frame_type: "approval.inbox.resolve";
  threadId: string;
  requestId: string;
  allow: boolean;
  message?: string;
  rememberEntry?: string;
  rememberScope?: "session" | "persistent";
}
