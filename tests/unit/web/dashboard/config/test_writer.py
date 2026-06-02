"""manage-config-tab dev-checklist #3 — writer.py 单元测试。

覆盖 README 列出的 6 类场景：

1. happy path：改 ``model.temperature``，逐行比对注释/空行保留
2. mtime 冲突 → :class:`ConflictError`，原文件不动
3. pydantic 校验失败（``temperature=99``） → :class:`ValidationFailedError`，原文件不动
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

from web.dashboard.config.writer import (
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
# kongming-agent 测试用最小化配置
# 本文件由 test_writer.py 生成；改了它要回查测试断言。

model:
  # 模型名（必填）
  name: gpt-4o-mini
  # 本地服务 base_url，跳过 api_key 校验
  base_url: http://127.0.0.1:1234/v1
  # 采样温度，[0, 2] 之间
  temperature: 0.6

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
# 1) happy path：改 model.temperature，注释 + 空行保留
# ---------------------------------------------------------------------------


def test_happy_path_preserves_comments_and_blank_lines(yaml_file: Path) -> None:
    expected_mtime = yaml_file.stat().st_mtime

    # 让 mtime 有可观察的间隔（避免 1ns 精度下 new_mtime == expected_mtime 误判）
    time.sleep(0.01)

    result = round_trip_update(
        yaml_file,
        patch=[PatchItem(path="model.temperature", value=0.9)],
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
    assert any("temperature: 0.9" in ln for ln in new_lines), "temperature 没改成 0.9"
    assert not any("temperature: 0.6" in ln for ln in new_lines), "旧 temperature 残留"

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
            patch=[PatchItem(path="model.temperature", value=0.9)],
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

    # 99 超出 [0, 2] 范围
    with pytest.raises(ValidationFailedError) as excinfo:
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="model.temperature", value=99)],
            expected_mtime=expected_mtime,
        )

    assert excinfo.value.errors, "应当返回非空 errors"
    # 至少一条错误指向 model.temperature
    paths = [e.get("path", "") for e in excinfo.value.errors]
    assert any("temperature" in p for p in paths), f"errors 中应含 temperature 路径，得到 {paths}"

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
            PatchItem(path="model.temperature", value=1.5),
            PatchItem(path="runner.max_turns", value=42),
        ],
        expected_mtime=expected_mtime,
    )

    assert isinstance(result, WriteResult)
    new_text = yaml_file.read_text(encoding="utf-8")
    assert "temperature: 1.5" in new_text
    assert "max_turns: 42" in new_text

    # 注释依然在
    assert "# 模型名（必填）" in new_text
    assert "# 单轮最大 turn 数" in new_text


# ---------------------------------------------------------------------------
# 边界：FileNotFoundError 透传
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        round_trip_update(
            nonexistent,
            patch=[PatchItem(path="model.temperature", value=0.9)],
            expected_mtime=0.0,
        )


# ---------------------------------------------------------------------------
# 边界：path 试图穿过非 dict 节点
# ---------------------------------------------------------------------------


def test_traverse_through_scalar_raises(yaml_file: Path) -> None:
    """``model.temperature.bogus`` 中 temperature 是数字，不能再下钻。"""
    expected_mtime = yaml_file.stat().st_mtime
    original_text = yaml_file.read_text(encoding="utf-8")

    with pytest.raises(ValidationFailedError):
        round_trip_update(
            yaml_file,
            patch=[PatchItem(path="model.temperature.bogus", value=1)],
            expected_mtime=expected_mtime,
        )

    # 原文件不动
    assert yaml_file.read_text(encoding="utf-8") == original_text
