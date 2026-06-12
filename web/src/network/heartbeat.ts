/**
 * kongming-agent web-network-layer v0.1 · Heartbeat 前端实现
 *
 * ## 这份文件是什么
 *
 * **单连接心跳状态机**——一个 `Heartbeat` 实例对应一条 socket。`NetworkManager`
 * 维护 `Map<connId, Heartbeat>`，N 条连接 = N 个并发 Heartbeat 实例。
 *
 * 设计文档：
 * - `docs/web-network-layer-v0.1/02-module-breakdown.md`（接口草图）
 * - `docs/web-network-layer-v0.1/04-data-and-state.md`（内部字段 + HeartbeatConfig 真源）
 *
 * ## 核心约束
 *
 * - **多实例并发安全**：每个实例自管 timer 句柄 / 计数器，互不污染。
 * - **纯被动设计**：Heartbeat **不持有** socket 引用、**不知道** channel 类型，
 *   只通过 `HeartbeatHooks` 4 个回调与外界交互。
 * - **数值真源**：`HeartbeatConfig` 由 manager 注入来自 `useHeartbeatConfig` 的真值，
 *   本模块**严禁**立默认常量。
 */

// ---------------------------------------------------------------------------
// 公共类型
// ---------------------------------------------------------------------------

/**
 * Heartbeat 与外界交互的 4 个回调。
 *
 * - `isOpen`            —— 底层 socket 是否可写（重连过渡期可能 false）
 * - `sendPing`          —— 发一帧 ping；ts 由 Heartbeat 生成传入
 * - `closeForReconnect` —— 连续丢 `maxMissed` 次时调；由 manager 触发重连
 * - `onLatency`         —— RTT 回调（可选）；handlePong 时调，参数为 ms
 */
export interface HeartbeatHooks {
  isOpen(): boolean;
  sendPing(ts: number): void;
  closeForReconnect(): void;
  onLatency?(ms: number): void;
}

/**
 * Heartbeat 三参数；**不设默认值**，必须由 manager 从 `useHeartbeatConfig` 注入。
 *
 * 真源：`cfg.web.ws_heartbeat_interval_ms` / `ws_heartbeat_timeout_ms` /
 * `ws_heartbeat_max_missed`。
 */
export interface HeartbeatConfig {
  /** 前台 ping 间隔，毫秒（来源 cfg.web.ws_heartbeat_interval_ms，默认 30000） */
  intervalMs: number;
  /**
   * 后台 ping 间隔，毫秒（来源 cfg.web.ws_heartbeat_background_interval_ms，默认 60000）。
   *
   * 浏览器 tab 切到后台时切换为此间隔。Chrome 后台节流 setInterval 到 ≥1min，
   * 前台 30s 在后台变成噪音 + RTT 测出来是节流延迟不是真网络 RTT。
   * 用更稀疏的间隔减少误导；切回前台时切回 `intervalMs` + 立即 probe。
   */
  backgroundIntervalMs: number;
  /** 单次 pong 超时，毫秒（来源 cfg.web.ws_heartbeat_timeout_ms） */
  timeoutMs: number;
  /** 连续丢几次判死 → closeForReconnect（来源 cfg.web.ws_heartbeat_max_missed） */
  maxMissed: number;
}

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

/**
 * 单连接心跳状态机。NetworkManager 在每次新建 socket 时 `new Heartbeat(config, hooks)`，
 * socket open 后调 `start()`，close 时调 `stop()`，收到 pong 帧时调 `handlePong(ts)`。
 */
export class Heartbeat {
  private readonly _config: HeartbeatConfig;
  private readonly _hooks: HeartbeatHooks;

  /** setInterval 句柄；null 表示 stop 状态 */
  private _heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  /** 单次 pong 超时 setTimeout 句柄；null 表示当前没 pending ping */
  private _pongTimer: ReturnType<typeof setTimeout> | null = null;
  /** 连续未回的 pong 次数 */
  private _missedPongs = 0;
  /** 最近一次未确认 ping 的时间戳（用于 RTT 计算回填校验） */
  private _pendingPingTs: number | null = null;
  /** 最近一次 RTT；尚未握过手时为 null */
  private _latencyMs: number | null = null;
  /** 状态机是否在跑（防 start 重入） */
  private _started = false;
  /** 当前 setInterval 用的 ms 值；setBackgrounded 切换前台/后台时变 */
  private _currentIntervalMs: number;

  constructor(config: HeartbeatConfig, hooks: HeartbeatHooks) {
    this._config = config;
    this._hooks = hooks;
    this._currentIntervalMs = config.intervalMs;
  }

  /**
   * 启动心跳：注册 `setInterval(intervalMs)` 周期发 ping。
   *
   * 幂等：已 started 的实例再调 start 直接返回，不会双注册 timer。
   */
  start(): void {
    if (this._started) return;
    this._started = true;
    // 防御：构造期到 start 之间外部可能复用实例，清掉残留
    this._clearTimers();
    this._missedPongs = 0;
    this._pendingPingTs = null;
    this._heartbeatTimer = setInterval(() => {
      this._sendPing();
    }, this._currentIntervalMs);
  }

  /**
   * 切换前台 / 后台模式：使用对应 interval 重启 setInterval。
   *
   * - `hidden=true`：用 `backgroundIntervalMs`（默认 60s，更稀疏减少节流噪音）
   * - `hidden=false`：用 `intervalMs`（默认 30s，前台正常频率）
   *
   * 若当前未 start（stop 状态）→ 只更新 `_currentIntervalMs`，下次 start 生效。
   * 若当前已 start → 立刻重启 setInterval 切到新 interval；正在 pending 的 pong
   * 超时计时不变（避免重启时把刚发的 ping 状态清掉）。
   *
   * @param hidden 来自 `document.hidden`：true=切到后台，false=切回前台
   */
  setBackgrounded(hidden: boolean): void {
    const nextInterval = hidden
      ? this._config.backgroundIntervalMs
      : this._config.intervalMs;
    if (nextInterval === this._currentIntervalMs) return;
    this._currentIntervalMs = nextInterval;
    if (!this._started) return;
    // 重启 setInterval 切到新频率；不动 _pongTimer / _pendingPingTs / _missedPongs
    if (this._heartbeatTimer !== null) {
      clearInterval(this._heartbeatTimer);
    }
    this._heartbeatTimer = setInterval(() => {
      this._sendPing();
    }, this._currentIntervalMs);
  }

  /**
   * 停止心跳：清理 interval / pong-timeout / 计数器 / pending ts。
   *
   * 幂等；socket close / channel close 时调。
   */
  stop(): void {
    this._started = false;
    this._clearTimers();
    this._missedPongs = 0;
    this._pendingPingTs = null;
  }

  /**
   * 收到 pong 帧时调用。
   *
   * - 清 pong 超时 timer
   * - 重置 `_missedPongs = 0`
   * - 计算 RTT = `Date.now() - ts` → 写 `_latencyMs` + 调 `hooks.onLatency?(rtt)`
   *
   * @param ts 此 pong 对应的 ping 时间戳（server 原样 echo 回来）
   */
  handlePong(ts: number): void {
    // 收到任意 pong 都视作 server 端活着；清掉 pong 超时 timer
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
    this._missedPongs = 0;
    // 如果 ts 跟最近一次 pending ping 不匹配，仍接受（server echo 可能延迟若干轮）
    // 这里读字段一次，避免 TS noUnusedLocals 抱怨；不强制相等。
    const expected = this._pendingPingTs;
    if (expected !== null && expected > ts) {
      // 防御性日志：server echo 比客户端发出还早 → 时钟问题
      console.warn(
        "[Heartbeat] pong ts < pending ts; clock skew?",
        { expected, ts },
      );
    }
    this._pendingPingTs = null;
    const rtt = Date.now() - ts;
    // 防御：负 RTT（时钟回拨 / ts 篡改）截到 0；超大 RTT 仍如实上报供诊断
    this._latencyMs = rtt < 0 ? 0 : rtt;
    if (this._hooks.onLatency) {
      try {
        this._hooks.onLatency(this._latencyMs);
      } catch (err) {
        // listener 抛错不应传染心跳；上层 manager 可能有 zustand setter 之类，
        // 测试环境也可能拿 onLatency 抛错验证健壮性
        console.error("[Heartbeat] onLatency listener threw", err);
      }
    }
  }

  /**
   * 立即发一帧 ping（不等下一个 interval）。
   *
   * 用途：visibilitychange 切回前台时 manager 对所有 Heartbeat 调 `probe()`，
   * 立即探测连接是否还活着。stop 状态下 probe 是 no-op。
   */
  probe(): void {
    if (!this._started) return;
    this._sendPing();
  }

  /**
   * 最近一次 RTT，毫秒；尚未握过手 / 已 stop 时返回 null。
   */
  get latencyMs(): number | null {
    return this._latencyMs;
  }

  // -------------------------------------------------------------------------
  // 内部实现
  // -------------------------------------------------------------------------

  /**
   * 发一帧 ping + 注册 pong 超时 timer。
   *
   * - socket 不 open 时直接 skip（不增 missed 计数；让重连流程接管判定）
   * - 已有 pendingPingTs 时不再发新 ping，避免双重 timer 叠加
   */
  private _sendPing(): void {
    if (!this._hooks.isOpen()) return;
    if (this._pongTimer !== null) {
      // 上一帧 ping 的 pong 超时未到，先不发新 ping（避免双 timer 触发误判死）
      return;
    }
    const ts = Date.now();
    this._pendingPingTs = ts;
    try {
      this._hooks.sendPing(ts);
    } catch (err) {
      // 底层 send 失败（如 readyState 异常）：清状态，让重连接管
      console.error("[Heartbeat] sendPing threw", err);
      this._pendingPingTs = null;
      return;
    }
    this._pongTimer = setTimeout(() => {
      this._onPongTimeout();
    }, this._config.timeoutMs);
  }

  /**
   * 单次 pong 超时回调：累加 missedPongs；达到上限调 closeForReconnect。
   */
  private _onPongTimeout(): void {
    this._pongTimer = null;
    this._pendingPingTs = null;
    this._missedPongs += 1;
    if (this._missedPongs >= this._config.maxMissed) {
      try {
        this._hooks.closeForReconnect();
      } catch (err) {
        console.error("[Heartbeat] closeForReconnect threw", err);
      }
      // 由 manager 在 onclose 里调 stop()；这里不主动 stop 防 stop 抢先把
      // _missedPongs 清零导致后续诊断丢失。
    }
  }

  private _clearTimers(): void {
    if (this._heartbeatTimer !== null) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  }
}
