"""roundtable_review 子 agent prompt 构造。

本脚本负责生成独立评审、交叉质询和仲裁三个阶段的中文 prompt。
作用是把同一份 ReviewBoard、源文件清单、角色职责和 JSON 输出契约稳定传给子 agent。
关键执行流程：策略按阶段调用 build_independent_prompt、build_rebuttal_prompt、
build_arbiter_prompt，子 agent 在 scoped workdir 内读取 input/source 和 board snapshot。
关键函数：build_independent_prompt 生成 reviewer 首轮任务，build_rebuttal_prompt 生成质询任务，
build_arbiter_prompt 生成最终报告任务。
"""

from __future__ import annotations

from application.agent_workflows.strategies.roundtable_review.contracts import (
    ReviewerSpec,
    RoundtableReviewSpec,
)


def build_independent_prompt(
    *,
    spec: RoundtableReviewSpec,
    reviewer: ReviewerSpec,
    per_agent_token_budget: int,
) -> str:
    """生成独立评审 prompt，输入为 spec/reviewer/预算，输出为任务文本。"""
    return "\n".join(
        [
            f"你是 {reviewer.title}（{reviewer.agent_id}）。",
            f"关注点：{reviewer.focus}",
            f"职责：{reviewer.instructions}",
            f"本轮 token 预算上限：约 {per_agent_token_budget} tokens。",
            "",
            "评审主题：",
            spec.topic,
            "",
            "评审目标：",
            spec.objective,
            "",
            "可读取材料：",
            "- input/source/ 下的源码或文档副本",
            "- input/input_manifest.json",
            "- input/review_board_snapshot.md",
            "",
            "任务：独立审查材料，暂时只输出你的发现。",
            "每条 finding 必须包含具体证据，证据优先使用文件路径和行号。",
            "",
            "只输出 JSON 对象，格式：",
            "{",
            f'  "agent": "{reviewer.agent_id}",',
            '  "findings": [',
            "    {",
            '      "severity": "P1",',
            '      "claim": "模块边界混合了调度逻辑和持久化逻辑",',
            '      "evidence": [{"type": "code", "path": "input/source/src/example.py", "lines": "10-30"}],',
            '      "risk": "后续扩展会放大耦合",',
            '      "suggestion": "拆出清晰的门户和内部实现",',
            '      "confidence": 0.8',
            "    }",
            "  ]",
            "}",
        ]
    )


def build_rebuttal_prompt(
    *,
    spec: RoundtableReviewSpec,
    reviewer: ReviewerSpec,
    round_index: int,
    per_agent_token_budget: int,
) -> str:
    """生成交叉质询 prompt，输入为 spec/reviewer/轮次/预算，输出为任务文本。"""
    return "\n".join(
        [
            f"你是 {reviewer.title}（{reviewer.agent_id}）。",
            f"关注点：{reviewer.focus}",
            f"本轮是第 {round_index} 轮，独立分析计为第 1 轮。",
            f"本轮 token 预算上限：约 {per_agent_token_budget} tokens。",
            "",
            "读取 input/review_board_snapshot.md 中的 claims 和已有 rebuttals。",
            "只允许三类发言：support、refute、supplement。",
            "support：同意某个 claim，并补充额外证据。",
            "refute：反对某个 claim，并说明它忽略的约束或证据。",
            "supplement：承认 claim 成立，并调整范围、等级或建议。",
            "",
            "只输出 JSON 对象，格式：",
            "{",
            f'  "agent": "{reviewer.agent_id}",',
            '  "comments": [',
            "    {",
            '      "type": "support",',
            '      "target_claim_id": "C-001",',
            '      "comment": "同意该 claim，另有证据显示同一问题影响测试入口",',
            '      "evidence": [{"type": "code", "path": "input/source/tests/test_example.py", "lines": "15-40"}],',
            '      "severity_adjustment": null,',
            '      "confidence": 0.75',
            "    }",
            "  ]",
            "}",
            "",
            "评审主题：",
            spec.topic,
        ]
    )


def build_arbiter_prompt(
    *,
    spec: RoundtableReviewSpec,
    per_agent_token_budget: int,
) -> str:
    """生成仲裁 prompt，输入为 spec 和预算，输出为最终总结任务文本。"""
    return "\n".join(
        [
            "你是 Multi-Agent Roundtable Review 的 Arbiter Agent。",
            "你负责读取 ReviewBoard，区分共识、分歧、风险和可执行修改建议。",
            f"本轮 token 预算上限：约 {per_agent_token_budget} tokens。",
            "",
            "读取 input/review_board_snapshot.md、input/source/ 和 input/input_manifest.json。",
            "读完 input/review_board_snapshot.md 和必要 source 后，停止调用工具，直接输出最终 Markdown 报告。",
            "最终报告只通过本次 assistant 最终回复返回。",
            "每个必要文件读取一次即可。",
            "输出 Markdown 报告，必须包含以下 6 节：",
            "1. 共识问题",
            "2. 主要分歧",
            "3. 高优先级风险",
            "4. 建议修改方案",
            "5. 需要人工确认的问题",
            "6. 可直接交给开发 Agent 的任务清单",
            "",
            "每个结论都绑定 claim_id 或 evidence。没有足够证据的项进入“需要人工确认的问题”。",
            "",
            "评审主题：",
            spec.topic,
            "",
            "评审目标：",
            spec.objective,
        ]
    )


__all__ = [
    "build_arbiter_prompt",
    "build_independent_prompt",
    "build_rebuttal_prompt",
]
