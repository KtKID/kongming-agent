"""workflow prompt catalog 单元测试。

本脚本验证 workflow strategy description 到 system prompt listing 的投影合同。
作用是锁定 catalog 全量读取、when_to_use 映射、稳定排序、字段裁剪和 hash 稳定性。
关键执行流程：构造 fake description -> 生成 snapshot -> render listing -> 断言文本和 hash。
关键函数：test_prompt_catalog_projects_descriptions_to_sorted_entries 验证映射，
test_workflow_prompt_listing_formatter_keeps_listing_short_and_stable 验证 formatter。
"""

from __future__ import annotations

import pytest

from application.agent_workflows.prompt_catalog import (
    WORKFLOW_PROMPT_ORIGIN,
    WorkflowPromptCatalogManager,
    WorkflowPromptListingFormatter,
)
from application.agent_workflows.strategies.description import (
    WorkflowStrategyCatalogEntry,
    WorkflowStrategyDescription,
    WorkflowStrategyInputField,
)


def _description(
    mode: str,
    title: str,
    *when_to_use: str,
) -> WorkflowStrategyDescription:
    """构造测试 description，输入为 mode/title/场景，输出完整策略说明。"""
    return WorkflowStrategyDescription(
        mode=mode,
        title=title,
        status="available",
        runnable=True,
        summary=f"{title} summary should stay out of listing",
        when_to_use=when_to_use,
        warnings=(f"{title} warning should stay out of listing",),
        inputs=(
            WorkflowStrategyInputField(
                name="payload_field",
                required=True,
                type_label="string",
                description=f"{title} input should stay out of listing",
                example="secret-example",
            ),
        ),
        outputs=(f"{title} output should stay out of listing",),
        examples=({"payload_field": "secret-example"},),
        depends_on=("future-capability",),
    )


def test_prompt_catalog_projects_descriptions_to_sorted_entries() -> None:
    """验证 description 投影，输入为乱序 description，输出按 mode 排序的 entries。"""
    snapshot = WorkflowPromptCatalogManager().snapshot_from_descriptions(
        [
            _description("zeta", "Zeta", "zeta scenario"),
            _description("alpha", "Alpha", " alpha scenario ", ""),
        ]
    )

    assert snapshot.registry_modes == ("alpha", "zeta")
    assert [entry.mode for entry in snapshot.entries] == ["alpha", "zeta"]
    assert snapshot.entries[0].title == "Alpha"
    assert snapshot.entries[0].usage_scenarios == ("alpha scenario",)
    assert snapshot.source_version == "workflow-prompt-catalog-source-v1"
    assert len(snapshot.listing_hash) == 16


def test_prompt_catalog_default_registry_exposes_all_registered_workflows() -> None:
    """验证默认 registry 全量读取，输入为空，输出当前默认 workflow mode 集合。"""
    snapshot = WorkflowPromptCatalogManager().snapshot_default_workflows()

    assert set(snapshot.registry_modes) >= {
        "deep_research",
        "map_reduce",
        "parallel",
        "roundtable_review",
        "task_flow",
    }
    assert all(entry.usage_scenarios for entry in snapshot.entries)


class _FakeWorkflowManager:
    """测试用 workflow manager，输入为空，输出可列举和 describe 的策略。"""

    def __init__(self) -> None:
        """初始化 fake manager，输入为空，输出带调用记录的 manager。"""
        self.describe_calls: list[str] = []

    def list_workflow_strategies(self) -> tuple[WorkflowStrategyCatalogEntry, ...]:
        """列出 workflow，输入为空，输出乱序 catalog entries。"""
        return (
            _description("zeta", "Zeta", "zeta scenario").catalog_entry(),
            _description("alpha", "Alpha", "alpha scenario").catalog_entry(),
        )

    def describe_workflow_strategy(self, mode: str) -> WorkflowStrategyDescription:
        """描述 workflow，输入为 mode，输出对应 description 并记录调用。"""
        self.describe_calls.append(mode)
        return _description(mode, mode.title(), f"{mode} scenario")


def test_prompt_catalog_snapshots_runtime_manager_by_describing_listed_modes() -> None:
    """验证运行期 manager 路径，输入为 fake manager，输出全量 describe 后的快照。"""
    fake_manager = _FakeWorkflowManager()

    snapshot = WorkflowPromptCatalogManager().snapshot_from_manager(fake_manager)

    assert fake_manager.describe_calls == ["zeta", "alpha"]
    assert snapshot.registry_modes == ("alpha", "zeta")
    assert [entry.title for entry in snapshot.entries] == ["Alpha", "Zeta"]
    assert snapshot.entries[0].usage_scenarios == ("alpha scenario",)


def test_workflow_prompt_listing_formatter_keeps_listing_short_and_stable() -> None:
    """验证 formatter 字段裁剪与稳定性，输入为等价快照，输出相同 text/hash。"""
    manager = WorkflowPromptCatalogManager()
    formatter = WorkflowPromptListingFormatter()
    snapshot = manager.snapshot_from_descriptions(
        [
            _description("beta", "Beta", "beta scenario"),
            _description("alpha", "Alpha", "alpha scenario"),
        ]
    )
    same_snapshot = manager.snapshot_from_descriptions(
        [
            _description("alpha", "Alpha", "alpha scenario"),
            _description("beta", "Beta", "beta scenario"),
        ]
    )

    render = formatter.render(snapshot)
    same_render = formatter.render(same_snapshot)

    assert render.origin == WORKFLOW_PROMPT_ORIGIN
    assert render.text == same_render.text
    assert render.listing_hash == same_render.listing_hash
    assert render.template_version == "workflow-prompt-catalog-template-v1"
    assert 'describe_agent_workflow_strategy(mode="xx")' in render.text
    assert "run_agent_workflow" in render.text
    assert render.text.index("mode: alpha") < render.text.index("mode: beta")
    assert "alpha scenario" in render.text
    assert "summary should stay out" not in render.text
    assert "warning should stay out" not in render.text
    assert "input should stay out" not in render.text
    assert "secret-example" not in render.text
    assert "output should stay out" not in render.text


def test_prompt_catalog_rejects_empty_when_to_use() -> None:
    """验证空使用场景被拒绝，输入为空 when_to_use，输出 ValueError。"""
    with pytest.raises(ValueError, match="when_to_use"):
        WorkflowPromptCatalogManager().snapshot_from_descriptions(
            [_description("empty", "Empty", "  ")]
        )
