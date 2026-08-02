"""manage-config-tab dev-checklist #3 — writer.py 单元测试。

覆盖 README 列出的 6 类场景：

1. happy path：改 ``model.reasoning_effort``，逐行比对注释/空行保留
2. mtime 冲突 → :class:`ConflictError`，原文件不动
3. pydantic 校验失败（非法 reasoning effort） → :class:`ValidationFailedError`，原文件不动
4. 嵌套字段（``evolution.memory.enabled``）改完仍保留注释
5. 路径不存在（``nonexistent.foo``） → :class:`ValidationFailedError`
6. 多字段一次写入（两个 patch 同时生效）

约束：

- 全部用 ``tmp_path`` fixture，禁止读写 ``config/setting.yaml`` 真文件；
- 不允许 ``python -c`` 一次性验证；所有断言写进 pytest。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from infrastructure.config.writer import (
    ConflictError,
    PatchItem,
    ValidationFailedError,
    WriteResult,
    round_trip_update,
)

# ---------------------------------------------------------------------------
# fixtures：极简但能通过 Config pydantic 校验的 yaml 原文
# ---------------------------------------------------------------------------

# 含中文注释、空行、多层嵌套，模拟真实 setting.yaml 的注释密度。
# 不引用任何外部 yaml；保留所有"格式细节"用于注释保留断言。
_MINI_YAML = """\
config_schema_version: v0.6
# kongming-agent 测试用最小化配置
# 本文件由 test_writer.py 生成；改了它要回查测试断言。

model:
  # 默认模型 preset（必填）
  preset_id: local-gemma-4-e4b-it
  # 默认推理档位
  reasoning_effort: medium

runner:
  # 单轮最大 turn 数
  max_turns: 10

evolution:
  memory:
    # 是否启用 memory（bool）
    enabled: true
    inject_prompt: false
"""


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    """生成一份临时 setting.yaml；返回绝对路径。"""
    p = tmp_path / "setting.yaml"
    p.write_text(_MINI_YAML, encoding="utf-8")
    return p


def _read_lines(p: Path) -> list[str]:
    """逐行读，保留 trailing newline。供注释保留断言。"""
    return p.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# 1) happy path：改 model.reasoning_effort，注释 + 空行保留
# ---------------------------------------------------------------------------


def test_happy_path_preserves_comments_and_blank_lines(yaml_file: Path) -> None:
    expected_mtime = yaml_file.stat().st_mtime

    # 让 mtime 有可观察的间隔（避免 1ns 精度下 new_mtime == expected_mtime 误判）
    time.sleep(0.01)

    result = round_trip_update(
        yaml_file,
        patch=[PatchItem(path="model.reasoning_effort", value="high")],
        expected_mtime=expected_mtime,
    )

    assert isinstance(result, WriteResult)
    assert result.new_mtime >= expected_mtime  # 文件已重写

    new_lines = _read_lines(yaml_file)
    old_lines = _MINI_YAML.splitlines()

    # 注释行全部保留（逐行比对所有 `#` 开头行）
    old_comments = [ln for ln in old_lines if ln.lstrip().startswith("#")]
    new_comments = [ln for ln in new_lines if ln.lstrip().startswith("#")]
    assert old_comments == new_comments, "注释行被改动了"

    # 空行数量保留（ruamel rt 模式应该保住）
    old_blank = sum(1 for ln in old_lines if not ln.strip())
    new_blank = sum(1 for ln in new_lines if not ln.strip())
    assert old_blank == new_blank, "空行数量变化"

    # 值确实改了
    assert any("reasoning_effort: high" in ln for ln in new_lines)
    assert not any("reasoning_effort: medium" in ln for ln in new_lines)

    # diff_lines 极小：温度行 1 个 - + 1 个 + = 2 行
    assert result.diff_lines <= 3, f"改 1 字段不应产生 >{result.diff_lines} 行差异"


# ---------------------------------------------------------------------------
# 2) mtime 冲突 → ConflictError，原文件不动
# ---------------------------------------------------------------------------


def test_mtime_conflict_raises_and_does_not_touch_file(yaml_file: Path) -> None:
    original_text = yaml_file.read_text(encoding="utf-8")
    stale_mtime = yaml_file.stat().st_mtime - 1000.0  # 故意造一个旧 mtime

    with pytest.raises(ConflictError) as excinfo:
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="model.reasoning_effort", value="high")],
            expected_mtime=stale_mtime,
        )

    assert excinfo.value.expected_mtime == stale_mtime
    assert excinfo.value.path == yaml_file

    # 文件未被改动（包括 mtime / 内容）
    assert yaml_file.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# 3) pydantic 校验失败 → ValidationFailedError，原文件不动
# ---------------------------------------------------------------------------


def test_pydantic_validation_failure_rolls_back(yaml_file: Path) -> None:
    original_text = yaml_file.read_text(encoding="utf-8")
    expected_mtime = yaml_file.stat().st_mtime

    # ultra 超出 reasoning effort 枚举范围
    with pytest.raises(ValidationFailedError) as excinfo:
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="model.reasoning_effort", value="ultra")],
            expected_mtime=expected_mtime,
        )

    assert excinfo.value.errors, "应当返回非空 errors"
    # 至少一条错误指向 model.reasoning_effort
    paths = [e.get("path", "") for e in excinfo.value.errors]
    assert any("reasoning_effort" in p for p in paths), (
        f"errors 中应含 reasoning_effort 路径，得到 {paths}"
    )

    # 原文件 + tmp 都不留
    assert yaml_file.read_text(encoding="utf-8") == original_text
    siblings = list(yaml_file.parent.iterdir())
    tmp_residues = [p for p in siblings if p.name.startswith(yaml_file.name + ".tmp.")]
    assert not tmp_residues, f"临时文件未清理：{tmp_residues}"


# ---------------------------------------------------------------------------
# 4) 嵌套字段：evolution.memory.enabled
# ---------------------------------------------------------------------------


def test_nested_field_update_preserves_comments(yaml_file: Path) -> None:
    expected_mtime = yaml_file.stat().st_mtime
    time.sleep(0.01)

    result = round_trip_update(
        yaml_file,
        patch=[PatchItem(path="evolution.memory.enabled", value=False)],
        expected_mtime=expected_mtime,
    )

    assert isinstance(result, WriteResult)
    new_text = yaml_file.read_text(encoding="utf-8")
    # 值翻成 false
    assert "enabled: false" in new_text, "evolution.memory.enabled 没改成 false"
    # 嵌套字段的内联注释保留
    assert "# 是否启用 memory（bool）" in new_text, "嵌套字段的同级注释丢失"
    # 其他字段不动
    assert "inject_prompt: false" in new_text


# ---------------------------------------------------------------------------
# 5) 路径不存在 → ValidationFailedError
# ---------------------------------------------------------------------------


def test_unknown_path_raises_validation_failure(yaml_file: Path) -> None:
    original_text = yaml_file.read_text(encoding="utf-8")
    expected_mtime = yaml_file.stat().st_mtime

    with pytest.raises(ValidationFailedError) as excinfo:
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="nonexistent.foo", value=123)],
            expected_mtime=expected_mtime,
        )

    paths = [e.get("path", "") for e in excinfo.value.errors]
    assert any("nonexistent" in p for p in paths), f"errors 中应含 nonexistent，得到 {paths}"

    # 原文件不动
    assert yaml_file.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# 6) 多字段一次性写
# ---------------------------------------------------------------------------


def test_multi_field_patch_applies_all(yaml_file: Path) -> None:
    expected_mtime = yaml_file.stat().st_mtime
    time.sleep(0.01)

    result = round_trip_update(
        yaml_file,
        patch=[
            PatchItem(path="model.reasoning_effort", value="low"),
            PatchItem(path="runner.max_turns", value=42),
        ],
        expected_mtime=expected_mtime,
    )

    assert isinstance(result, WriteResult)
    new_text = yaml_file.read_text(encoding="utf-8")
    assert "reasoning_effort: low" in new_text
    assert "max_turns: 42" in new_text

    # 注释依然在
    assert "# 默认模型 preset（必填）" in new_text
    assert "# 单轮最大 turn 数" in new_text


# ---------------------------------------------------------------------------
# 边界：FileNotFoundError 透传
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        round_trip_update(
            nonexistent,
            patch=[PatchItem(path="model.reasoning_effort", value="high")],
            expected_mtime=0.0,
        )


# ---------------------------------------------------------------------------
# 边界：path 试图穿过非 dict 节点
# ---------------------------------------------------------------------------


def test_traverse_through_scalar_raises(yaml_file: Path) -> None:
    """``model.reasoning_effort.bogus`` 穿过标量时必须失败。"""
    expected_mtime = yaml_file.stat().st_mtime
    original_text = yaml_file.read_text(encoding="utf-8")

    with pytest.raises(ValidationFailedError):
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="model.reasoning_effort.bogus", value=1)],
            expected_mtime=expected_mtime,
        )

    # 原文件不动
    assert yaml_file.read_text(encoding="utf-8") == original_text
