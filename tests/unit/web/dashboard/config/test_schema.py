"""manage-config-tab dev-checklist #9 — schema.py 字段元数据漂移防火墙。

本测试是 manage 配置页字段真源（``src/web/dashboard/config/schema.py``）的**漂移防火墙**：
当 ``src/config_loader/models.py`` 内任一 leaf 字段新增 / 改名 / 改类型 / 删除时，
schema.py 必须同步修改，否则 manage UI 与真实 pydantic schema 不一致。

加新字段流程：

1. 改 ``src/config_loader/models.py``（pydantic 真源）。
2. 同步改 ``src/web/dashboard/config/schema.py``（_FIELD_METAS 列表 + _GROUPS 若需新增）。
3. 跑本测试 ``uv run pytest tests/unit/web/dashboard/config/test_schema.py -v``，全过即可。

**覆盖范围限定**：一期 stage 1 只覆盖 16 个顶层模块。``sitian`` / ``workflow`` 两个
顶层模块**不在断言范围内**——它们由后续 stage 引入。本测试在 pydantic 覆盖断言里会
扁平化后**过滤掉 sitian.* / workflow.* 前缀**。

每个断言用独立 test 函数（pytest failure-locality 最佳实践，方便看哪条断言 fail）。
"""

from __future__ import annotations

from typing import Any

import pytest

from config_loader.models import Config, ModelConfig
from web.dashboard.config.schema import (
    FieldMeta,
    get_field_meta,
    list_field_metas,
    list_groups,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 一期不覆盖的顶层模块（后续 stage 接入）
_EXCLUDED_TOP_LEVEL_MODULES: frozenset[str] = frozenset({"sitian", "workflow"})

# group 期望顺序（与 schema._GROUPS 同步）
_EXPECTED_GROUP_IDS: list[str] = [
    "model",
    "runtime",
    "tool_approval",
    "safety",
    "host_observ",
]

# type 字段允许的闭集
_ALLOWED_TYPES: frozenset[str] = frozenset(
    {"string", "int", "float", "bool", "enum", "list", "dict"}
)

# 强制锁定（editable=False）的字段
_LOCKED_FIELDS: frozenset[str] = frozenset({"model.api_key", "web.dev_mode", "host.kind"})

# 已知 dict-leaf 字段（schema 视图把整段当 1 个 leaf，flatten 不递归展开）。
# 这两个字段是真 dict（不是嵌套 BaseModel），由用户在 yaml 内手工维护，
# UI 一期只读不暴露子字段编辑。
_DICT_LEAF_PATHS: frozenset[str] = frozenset(
    {
        "model.provider_routing",
        "model.reasoning_profiles",
    }
)


# ---------------------------------------------------------------------------
# Fixtures / 辅助
# ---------------------------------------------------------------------------


def flatten_dict(data: dict[str, Any], prefix: str = "") -> list[str]:
    """嵌套 dict 扁平化为 dot-path 列表。

    - 命中 ``_DICT_LEAF_PATHS`` 的字段整段算 1 个 leaf（不递归）。
    - 非空嵌套 dict 继续递归。
    - 空 dict / list / scalar / None 整段算 1 个 leaf。
    """
    paths: list[str] = []
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if path in _DICT_LEAF_PATHS:
            paths.append(path)
            continue
        if isinstance(v, dict) and v:
            paths.extend(flatten_dict(v, path))
        else:
            # 空 dict / list / scalar / None 整段算 1 个 leaf
            paths.append(path)
    return paths


@pytest.fixture
def metas() -> list[FieldMeta]:
    """schema 真源全部字段元数据。"""
    return list_field_metas()


@pytest.fixture
def groups() -> list[dict[str, str]]:
    """schema 真源 group 列表。"""
    return list_groups()


@pytest.fixture
def pydantic_leaf_paths() -> set[str]:
    """从 pydantic Config 真源扁平化得到的全部 leaf path 集合（已过滤排除模块）。

    构造方式：``Config()`` 实例化（``model`` 必填，给一份本地 endpoint 占位），
    其余子节走 pydantic ``Field(default_factory=...)`` 默认值——这正是漂移检测
    所需的"完整 schema"。
    """
    cfg = Config(
        model=ModelConfig(
            name="stub-model",
            base_url="http://127.0.0.1:1234/v1",
        )
    )
    dumped = cfg.model_dump(mode="json")
    all_paths = flatten_dict(dumped)
    return {p for p in all_paths if p.split(".", 1)[0] not in _EXCLUDED_TOP_LEVEL_MODULES}


# ---------------------------------------------------------------------------
# 断言 1：path 唯一性
# ---------------------------------------------------------------------------


def test_field_paths_unique(metas: list[FieldMeta]) -> None:
    paths = [m.path for m in metas]
    duplicates = {p for p in paths if paths.count(p) > 1}
    assert not duplicates, f"重复的字段 path：{sorted(duplicates)}"


# ---------------------------------------------------------------------------
# 断言 2：每个字段的 group 都在 list_groups() 内
# ---------------------------------------------------------------------------


def test_every_field_group_is_known(metas: list[FieldMeta], groups: list[dict[str, str]]) -> None:
    known_group_ids = {g["id"] for g in groups}
    bad = [(m.path, m.group) for m in metas if m.group not in known_group_ids]
    assert not bad, (
        f"以下字段的 group 不在 list_groups() 中（已知 group={sorted(known_group_ids)}）：{bad}"
    )


# ---------------------------------------------------------------------------
# 断言 3：5 个 group 正好且顺序匹配
# ---------------------------------------------------------------------------


def test_groups_match_expected_order(groups: list[dict[str, str]]) -> None:
    actual_ids = [g["id"] for g in groups]
    assert actual_ids == _EXPECTED_GROUP_IDS, (
        f"group id 列表或顺序错：expected={_EXPECTED_GROUP_IDS}, actual={actual_ids}"
    )


def test_groups_have_label(groups: list[dict[str, str]]) -> None:
    """每个 group 必须有非空 label（UI 显示用）。"""
    missing = [g["id"] for g in groups if not g.get("label", "").strip()]
    assert not missing, f"以下 group 缺 label：{missing}"


# ---------------------------------------------------------------------------
# 断言 4：enum 字段必带 enum 列表
# ---------------------------------------------------------------------------


def test_enum_fields_have_enum_values(metas: list[FieldMeta]) -> None:
    bad = [m.path for m in metas if m.type == "enum" and (m.enum is None or len(m.enum) == 0)]
    assert not bad, f"以下 enum 字段缺 enum 列表（或为空）：{bad}"


# ---------------------------------------------------------------------------
# 断言 5：type 必须在闭集内
# ---------------------------------------------------------------------------


def test_field_types_are_in_allowed_set(metas: list[FieldMeta]) -> None:
    bad = [(m.path, m.type) for m in metas if m.type not in _ALLOWED_TYPES]
    assert not bad, f"以下字段的 type 不在允许集 {sorted(_ALLOWED_TYPES)} 内：{bad}"


# ---------------------------------------------------------------------------
# 断言 6：pydantic 字段覆盖（核心断言）
# ---------------------------------------------------------------------------


def test_schema_covers_all_pydantic_fields(
    metas: list[FieldMeta], pydantic_leaf_paths: set[str]
) -> None:
    """pydantic 真源里的所有字段（排除 sitian/workflow）必须在 schema 中有对应 FieldMeta。

    允许 schema 多出字段（如显示用的虚拟分组项），但不允许少。
    """
    schema_paths = {m.path for m in metas}
    missing_in_schema = pydantic_leaf_paths - schema_paths
    extra_in_schema = schema_paths - pydantic_leaf_paths
    assert not missing_in_schema, (
        f"pydantic 真源有但 schema 缺失的字段（必须补到 schema.py 的 _FIELD_METAS）："
        f"{sorted(missing_in_schema)}；"
        f"schema 多出的字段（可能也需复查）：{sorted(extra_in_schema)}"
    )


# ---------------------------------------------------------------------------
# 断言 7：强制锁定字段必须 editable=False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locked_path", sorted(_LOCKED_FIELDS))
def test_locked_fields_not_editable(locked_path: str) -> None:
    meta = get_field_meta(locked_path)
    assert meta is not None, f"锁定字段 {locked_path!r} 在 schema 中不存在"
    assert meta.editable is False, (
        f"锁定字段 {locked_path!r} 必须 editable=False，实际 editable={meta.editable}"
    )


# ---------------------------------------------------------------------------
# 断言 8：list / dict 字段一期统一只读
# ---------------------------------------------------------------------------


def test_list_and_dict_fields_are_readonly(metas: list[FieldMeta]) -> None:
    bad = [m.path for m in metas if m.type in {"list", "dict"} and m.editable]
    assert not bad, f"以下 list/dict 字段在 schema 内被标 editable=True（一期统一只读）：{bad}"


# ---------------------------------------------------------------------------
# 断言 9：get_field_meta() 行为
# ---------------------------------------------------------------------------


def test_get_field_meta_returns_correct_field() -> None:
    meta = get_field_meta("model.temperature")
    assert meta is not None, "get_field_meta('model.temperature') 应返回非 None"
    assert meta.path == "model.temperature", (
        f"get_field_meta 返回的 path 与查询不一致：expected=model.temperature, actual={meta.path}"
    )


def test_get_field_meta_returns_none_for_unknown_path() -> None:
    assert get_field_meta("this.path.does.not.exist") is None, (
        "get_field_meta 对不存在的 path 应返回 None"
    )


# ---------------------------------------------------------------------------
# 额外健康检查：min/max_value 数值合理性
# ---------------------------------------------------------------------------


def test_min_max_value_consistency(metas: list[FieldMeta]) -> None:
    """如果同时设了 min 和 max，min 必须 ≤ max。"""
    bad = [
        (m.path, m.min_value, m.max_value)
        for m in metas
        if m.min_value is not None and m.max_value is not None and m.min_value > m.max_value
    ]
    assert not bad, f"以下字段 min_value > max_value：{bad}"


def test_min_max_value_only_on_numeric_types(metas: list[FieldMeta]) -> None:
    """min_value / max_value 只对 int / float 字段有意义。"""
    numeric_types = {"int", "float"}
    bad = [
        (m.path, m.type)
        for m in metas
        if (m.min_value is not None or m.max_value is not None) and m.type not in numeric_types
    ]
    assert not bad, f"以下非数值字段被设了 min_value/max_value（应只在 int/float 上用）：{bad}"
