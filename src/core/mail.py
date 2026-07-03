"""Mail — mailbox 传输信封（模块 C · Mailbox + agent_loop）。

功能：定义 agent 树里跨 agent 投递的统一消息信封 ``Mail``，作为每个
:class:`AgentCell` 的 ``mailbox``（``asyncio.Queue[Mail]``）的传输载体。
作用：把外部世界并发（用户随时说话、子 agent 随时完成、cron 随时触发）压成
目标 agent 的严格串行输入流；epoch 字段让 agent_loop 消费侧 epoch 门卫能
区分「旧世代内部投递（丢弃）」与「用户新输入（永不过期）」。
关键设计要点：
- ``Mail`` 是 frozen dataclass：信封一旦入队即不可变（避免中途被改 epoch）。
- ``enqueued_at_ms`` 用统一时间工具 :func:`core.clock.now_epoch_ms`（tz-aware）。
- payload 复用 :class:`core.message.Message`，provenance 走
  ``metadata['source_agent_id']`` 软键（不加 Message 字段，遵循 attachments 软契约先例）。
- 只 import core 自身类型（message / clock），不反向依赖 application/hosts
  （import-linter Contract 1：core 不可 import sibling 模块）。

事实源：``docs/spec/agent-tree-v0.1/04-data-and-state.md``（Mail 定义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.clock import now_epoch_ms
from core.message import Message

# Mail.kind 的封闭枚举：3 种投递来源。
MailKind = Literal["user_message", "child_result", "system_notice"]


@dataclass(frozen=True)
class Mail:
    """mailbox 传输信封（frozen，不可变）。

    一条 Mail 表示「某发送者 → 某 agent mailbox 的一次投递」。入队即冻结，
    epoch 字段携带产生该 mail 的 run 起始 epoch，供消费侧 epoch 门卫判定
    是否过期（旧世代内部投递丢弃，user_message 永不过期）。

    Attributes:
        kind: 投递种类。``user_message``（用户输入）/ ``child_result``
            （子 agent 完成回灌）/ ``system_notice``（cron / 系统通知）。
        sender: 发送者标识。子 agent 完成时 = 子 ``agent_id``；用户输入 = ``"user"``；
            cron / 调度 = ``"scheduler"``。
        recipient_agent_id: 接收者 agent_id（投进谁 mailbox 就是谁）。支持第三方
            投递（``sender != recipient``，如 cron 投给主 agent）。
        task_id: 关联 spawn 的 :class:`TaskRecord` task_id；``user_message`` = ``""``；
            ``child_result`` 时父消费可回溯是哪个 spawn 调用。
        epoch: 产生该 mail 的 run 起始 epoch。``user_message`` 永不过期（不参与
            门卫判定）；内部投递（``child_result`` / ``system_notice``）的 epoch
            在该 run 启动瞬间捕获，过期的旧世代投递会被消费侧丢弃。
        payload: 复用 :class:`Message`（无 author 字段；子 agent provenance 走
            ``payload.metadata['source_agent_id']`` 软键）。
        enqueued_at_ms: 入队时间戳（统一时间工具 :func:`now_epoch_ms`，tz-aware，
            非 naive ``time.time()``）。用于排障 / 日志。
    """

    kind: MailKind
    sender: str
    recipient_agent_id: str
    task_id: str
    epoch: int
    payload: Message
    enqueued_at_ms: int = field(default_factory=now_epoch_ms)


__all__ = ["Mail", "MailKind"]
