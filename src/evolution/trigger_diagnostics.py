"""Evolution 触发链诊断分类器——所有"该跑没跑 / 跑挂了"的路径统一归类落日志。

脚本功能与关键流程:
- 定义触发链全部阻断类别 ``TriggerBlockCategory``,分两档:
  真失败(ERROR)= 功能已开启但学习链没有走到底,需要人看;
  设计内跳过(INFO)= 节律 / 语义决定本轮不跑,属正常行为,留痕即可。
- ``_CATEGORY_LEVELS`` 把类别→日志级别的映射**代码写死**,不提供任何配置项:
  用户要求 evolution 开启后触发失败必须留下 error 日志,不允许静默消失;
  唯一豁免是 ``learning.enabled=false``(调用方在入口 no-op,不会走到本模块)。
- ``log_trigger_block()`` 是唯一日志出口,统一格式方便 grep:
  ``trigger blocked: category=<c> thread=<t> run=<r> detail=<d>``。

明示豁免(不进分类器):reviewer 被 CancelledError 收回只发生在 app 关闭
drain 超时的主动 cancel,属 shutdown 设计内收口,已有 drain_timeout 事件 +
``mark_review_result(cancelled)`` + ``review.failed(error_kind=cancelled)``
三重留痕,故不占用 ``trigger blocked:`` 统一格式。

关键函数:
- ``log_trigger_block``: 按类别查级别并写入 evolution 日志层级
  (``evolution.*`` logger 由 ``logging_setup`` 统一路由到
  ``<kongming_home>/logs/evolution.log``)。输入:类别 + thread/run 上下文 +
  可读 detail;输出:一条结构化日志行,无返回值。
"""

from __future__ import annotations

import logging
from typing import Literal

__all__ = ["TriggerBlockCategory", "log_trigger_block"]

logger = logging.getLogger(__name__)

# 触发链阻断类别。新增路径时必须同步登记在 _CATEGORY_LEVELS,否则 fail-fast。
TriggerBlockCategory = Literal[
    # --- 真失败(ERROR):功能开着,学习链却没走到底 ---
    "missing_evolution_write_tool",  # parent 工具表缺 evolution_write,复盘无法产出
    "empty_transcript_window",  # cadence 已命中但证据窗口为空,复盘无料可看
    "notify_exception",  # notify 触发链抛异常
    "reviewer_write_failed",  # reviewer 跑完但 evolution_write 未成功
    "reviewer_timeout_no_write",  # reviewer 超时且超时前未完成写入
    "reviewer_exception",  # reviewer 执行异常
    "reviewer_tool_contract_violation",  # reviewer LLM 返回未声明或超量工具调用
    # --- 设计内跳过(INFO):节律 / 语义决定本轮不复盘 ---
    "run_not_completed",  # 主 run 非 completed(cancelled / failed 不复盘)
    "reviewer_self_loop",  # reviewer 自己的 run 不再复盘,防自环
    "automatic_trigger_disabled",  # cadence 已关闭，显式 Tool 仍可用
    "below_min_user_turns",  # 冷启动保护未达标
    "cadence_not_due",  # 未逢 every_n_runs 节律
]

# 类别 → 日志级别,写死不可配置(见模块 docstring)。
_CATEGORY_LEVELS: dict[str, int] = {
    "missing_evolution_write_tool": logging.ERROR,
    "empty_transcript_window": logging.ERROR,
    "notify_exception": logging.ERROR,
    "reviewer_write_failed": logging.ERROR,
    "reviewer_timeout_no_write": logging.ERROR,
    "reviewer_exception": logging.ERROR,
    "reviewer_tool_contract_violation": logging.ERROR,
    "run_not_completed": logging.INFO,
    "reviewer_self_loop": logging.INFO,
    "automatic_trigger_disabled": logging.INFO,
    "below_min_user_turns": logging.INFO,
    "cadence_not_due": logging.INFO,
}


def log_trigger_block(
    category: TriggerBlockCategory,
    *,
    thread_id: str,
    run_id: str | None = None,
    detail: str = "",
    exc_info: bool = False,
) -> None:
    """触发链阻断的唯一日志出口。

    职责:按 ``_CATEGORY_LEVELS`` 写死的级别输出统一格式日志行。
    关键输入:``category`` 阻断类别;``thread_id``/``run_id`` 定位上下文;
    ``detail`` 人类可读补充;``exc_info=True`` 时附带当前异常堆栈。
    关键输出:一条 ``trigger blocked: ...`` 日志(ERROR 或 INFO)。

    未登记类别按 ERROR 处理(宁可吵也不静默)。
    """
    level = _CATEGORY_LEVELS.get(category, logging.ERROR)
    logger.log(
        level,
        "trigger blocked: category=%s thread=%s run=%s detail=%s",
        category,
        thread_id,
        run_id or "-",
        detail or "-",
        exc_info=exc_info,
    )
