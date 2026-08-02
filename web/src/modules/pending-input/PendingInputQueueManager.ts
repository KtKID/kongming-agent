import type {
  PendingInputCancelFrame,
  PendingInputChangedFrame,
  PendingInputDTO,
  PendingInputReorderFrame,
  PendingInputSendNowFrame,
  PendingInputSnapshotFrame,
  PendingInputStartedFrame,
  PendingInputUpdateFrame,
} from "@/protocol";

/**
 * pending input queue 的前端投影。
 *
 * 状态真源在后端 ThreadCell；前端保存 WS 快照投影和最近一次错误文案。
 * 组件层只读该结构，所有写入都经过本 manager 的 reducer 方法。
 */
export interface PendingInputQueueState {
  /** 当前队列归属的 thread；null 表示尚未绑定，可接受第一帧 snapshot。 */
  threadId: string | null;
  /** 尚未启动的输入项，顺序以服务端 snapshot 为准。 */
  items: PendingInputDTO[];
  /** 服务端声明的队列上限，用于展示容量和禁用继续排队。 */
  maxItems: number;
  /** 当前已启动 run 的 ID；后端暂时可能为空，前端需按 nullable 处理。 */
  activeRunId: string | null;
  /** 服务端队列版本；低版本帧会被丢弃，避免乱序 WS 帧回滚 UI。 */
  version: number;
  /** 最近被 started 帧确认启动的队列项 ID，用于 UI 关联过渡态。 */
  lastStartedId: string | null;
  /** 提交或队列操作失败的可见错误文案。 */
  lastError: string | null;
}

/**
 * pending input queue 的纯 reducer/帧构造器。
 *
 * 输入是当前 state 与服务端 WS frame，或用户操作产生的本地命令参数；
 * 输出是新的不可变 state，或发回服务端的 C2S frame。这里不访问 socket 和 React state。
 */
export class PendingInputQueueManager {
  /** 创建一个空队列状态；切换 thread 时由调用方传入新的 threadId。 */
  static empty(threadId: string | null = null): PendingInputQueueState {
    return {
      threadId,
      items: [],
      maxItems: 20,
      activeRunId: null,
      version: 0,
      lastStartedId: null,
      lastError: null,
    };
  }

  /** 接收服务端全量快照；thread 不匹配或版本过旧时保留本地状态。 */
  static applySnapshot(
    state: PendingInputQueueState,
    frame: PendingInputSnapshotFrame,
  ): PendingInputQueueState {
    if (!this.acceptsThread(state, frame.thread_id)) return state;
    if (frame.version < state.version) return state;
    return {
      ...state,
      threadId: frame.thread_id,
      items: frame.items,
      maxItems: frame.max_items,
      activeRunId: frame.active_run_id ?? null,
      version: frame.version,
      lastError: null,
    };
  }

  /** 接收服务端变更快照；items 以服务端完整列表覆盖本地列表。 */
  static applyChanged(
    state: PendingInputQueueState,
    frame: PendingInputChangedFrame,
  ): PendingInputQueueState {
    if (!this.acceptsThread(state, frame.thread_id)) return state;
    if (frame.version < state.version) return state;
    return {
      ...state,
      threadId: frame.thread_id,
      items: frame.items,
      maxItems: frame.max_items,
      activeRunId: frame.active_run_id ?? null,
      version: frame.version,
      lastError: null,
    };
  }

  /** 标记某个队列项已启动；先本地移除该项，再等待 changed 帧校准快照。 */
  static applyStarted(
    state: PendingInputQueueState,
    frame: PendingInputStartedFrame,
  ): PendingInputQueueState {
    if (!this.acceptsThread(state, frame.thread_id)) return state;
    if (frame.version < state.version) return state;
    return {
      ...state,
      threadId: frame.thread_id,
      items: state.items.filter((item) => item.id !== frame.pending_input_id),
      activeRunId: frame.run_id ?? state.activeRunId,
      version: frame.version,
      lastStartedId: frame.pending_input_id,
      lastError: null,
    };
  }

  /** 记录队列相关错误；不改 items，避免错误帧覆盖服务端队列真源。 */
  static withError(state: PendingInputQueueState, message: string): PendingInputQueueState {
    return { ...state, lastError: message };
  }

  /** 构造编辑帧；content 由调用方传入已 trim 的草稿文本。 */
  static buildUpdateFrame(id: string, content: string): PendingInputUpdateFrame {
    return {
      frame_type: "pending-input.update",
      pending_input_id: id,
      content,
    };
  }

  /** 构造删除帧；只作用于尚未启动的 pending input。 */
  static buildCancelFrame(id: string): PendingInputCancelFrame {
    return {
      frame_type: "pending-input.cancel",
      pending_input_id: id,
    };
  }

  /** 构造立即发送帧；后端决定插入当前 run 或启动下一轮 run。 */
  static buildSendNowFrame(id: string, requestId?: string | null): PendingInputSendNowFrame {
    return {
      frame_type: "pending-input.send-now",
      pending_input_id: id,
      request_id: requestId ?? null,
    };
  }

  /** 构造拖拽排序帧；orderedIds 是松手后的完整队列顺序。 */
  static buildReorderFrame(orderedIds: string[]): PendingInputReorderFrame {
    return {
      frame_type: "pending-input.reorder",
      ordered_ids: orderedIds,
    };
  }

  /** null threadId 表示等待首帧绑定；绑定后拒绝其他 thread 的乱入帧。 */
  private static acceptsThread(state: PendingInputQueueState, threadId: string): boolean {
    return state.threadId === null || state.threadId === threadId;
  }
}
