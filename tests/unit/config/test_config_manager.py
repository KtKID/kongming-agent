"""manage-config-tab dev-checklist #5 — :class:`ConfigManager` 单测。

覆盖 5 个公开方法的 9 类场景：

1. ``read_schema`` 字段数动态对齐 + groups 5 项
2. ``read_raw`` 返回 path / mtime / content
3. ``read_effective`` 区分 yaml / default 来源
4. ``read_effective`` env 覆盖命中 → ``sources="env"`` + 进入 ``env_overrides``
5. ``save_patch`` happy path：mtime 推进 + yaml 内容改 + restart_required_fields
6. ``save_patch`` 校验失败 → :class:`ValidationFailedError`
7. ``save_patch`` mtime 冲突 → :class:`ConflictError`
约束：

- 全部用 ``tmp_path`` fixture，禁止读写 ``config/setting.yaml`` 真文件；
- 用 ``monkeypatch.chdir(tmp_path)`` 避开仓库根 ``.env`` 注入；
- 用 ``monkeypatch.delenv`` 清理可能继承自宿主进程的 ``KONGMING_*`` env，
  避免父进程环境污染 sources 判定。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from infrastructure.config import schema as _schema
from infrastructure.config.manager import (
    ConfigManager,
    EffectiveResponse,
    RawResponse,
    SavePatchResponse,
    SchemaResponse,
)
from infrastructure.config.writer import (
    ConflictError,
    PatchItem,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

# 最小化 yaml：含 model（必填）+ 显式 model.reasoning_effort 用于断言 sources。
# 不写 runner.max_turns，让它走 pydantic 默认值 = 50。
_MINI_YAML = """\
config_schema_version: v0.6
# kongming-agent test config
model:
  preset_id: local-gemma-4-e4b-it
  reasoning_effort: medium
"""


# 所有可能影响 sources 判定的 KONGMING_* env（来自 loader._ENV_FIELD_PATHS
# + scheduler 额外 env）。每个测试通过 fixture 自动清理，保证测试间隔离。
# 实际只清理那些**测试断言会触达**的 env：MODEL.* / RUNNER.* / SCHEDULER.* /
# TRACE.* 等。若新增白名单字段，按需补齐这里。
_ENV_VARS_TO_CLEAN: tuple[str, ...] = (
    "KONGMING_MODEL_PRESET_ID",
    "KONGMING_MODEL_REASONING_EFFORT",
    "KONGMING_RUNNER_MAX_TURNS",
    "KONGMING_TRACE_RAW_LLM",
    "KONGMING_TRACE_AUTO_FLUSH",
    "KONGMING_TRACE_OUTPUT_PATH",
    "KONGMING_LOGGING_LEVEL",
    "KONGMING_WEB_ENABLED",
    "KONGMING_WEB_HOST",
    "KONGMING_WEB_PORT",
    "KONGMING_EVOLUTION_LEARNING_AUTO_TRIGGER_ENABLED",
    "KONGMING_EVOLUTION_LEARNING_PRESET_ID",
    "KONGMING_EVOLUTION_LEARNING_REASONING_EFFORT",
    "KONGMING_SCHEDULER_ENABLED",
    "KONGMING_SCHEDULER_INTERVAL",
    "KONGMING_SCHEDULER_MAX_INFLIGHT",
    "KONGMING_SCHEDULER_APPROVAL_MODE",
    "KONGMING_SCHEDULER_DEFAULT_MAX_TURNS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试自动清理可能继承自宿主进程的 KONGMING_* env，并禁用 .env 自动加载。

    项目根 .env 含 ``KONGMING_TRACE_RAW_LLM=1`` / ``KONGMING_MODEL_*`` 等，
    `load_config` 内部 `_maybe_load_env_file` → `dotenv.load_dotenv` 会
    从调用者源码路径向上搜，命中仓库根 .env，重新污染 os.environ。

    两步隔离：
    1. ``delenv`` 清掉宿主已设的 KONGMING_*
    2. monkeypatch `_maybe_load_env_file` 为 no-op，避免 .env 重新注入
    """
    for var in _ENV_VARS_TO_CLEAN:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "infrastructure.config.loader._maybe_load_env_file",
        lambda *_args, **_kwargs: {},
    )


@pytest.fixture
def yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """写一份临时 setting.yaml，并把 cwd 切到 tmp_path 避开仓库 .env 自动加载。

    ``load_dotenv`` 默认从 cwd 向上找 ``.env``，切到 tmp_path 后找不到任何
    .env 文件，对 os.environ 无副作用——配合 ``_clean_env`` 实现完整隔离。
    """
    p = tmp_path / "setting.yaml"
    p.write_text(_MINI_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return p


@pytest.fixture
def manager(yaml_file: Path, tmp_path: Path) -> ConfigManager:
    """返回 ConfigManager 实例。"""
    return ConfigManager(yaml_path=yaml_file, env_path=tmp_path / ".env")


# ---------------------------------------------------------------------------
# 1) read_schema
# ---------------------------------------------------------------------------


def test_read_schema_returns_all_fields_and_groups(manager: ConfigManager) -> None:
    """字段数 = schema.list_field_metas()；groups = schema.list_groups()。

    不 hardcode 字段总数（如 91 / 92 等）——schema 真源会增删字段，硬编码会
    误报。改用动态对齐：本测试只断言 manager 把 schema 的两个查询完整透传。
    """
    resp: SchemaResponse = manager.read_schema()
    assert len(resp.fields) == len(_schema.list_field_metas())
    assert resp.groups == _schema.list_groups()
    # 类型断言：response 的 fields 是 schema.FieldMeta 实例（透传，不复制）
    assert all(isinstance(f, _schema.FieldMeta) for f in resp.fields)
    # groups 形态：每条 {"id": ..., "label": ...}
    for g in resp.groups:
        assert set(g.keys()) >= {"id", "label"}


# ---------------------------------------------------------------------------
# 2) read_raw
# ---------------------------------------------------------------------------


def test_read_raw_returns_path_mtime_content(manager: ConfigManager, yaml_file: Path) -> None:
    resp: RawResponse = manager.read_raw()
    assert resp.path == str(yaml_file)
    assert resp.content.splitlines()[0].startswith("config_schema_version: v0.6")
    assert "# kongming-agent test config" in resp.content
    assert "reasoning_effort: medium" in resp.content
    # mtime 与 stat 一致（容忍浮点精度 1µs）
    assert abs(resp.mtime - yaml_file.stat().st_mtime) < 1e-6


# ---------------------------------------------------------------------------
# 3) read_effective：yaml 显式写 vs default
# ---------------------------------------------------------------------------


def test_read_effective_yaml_vs_default(manager: ConfigManager) -> None:
    """yaml 显式写的 model effort → source=yaml；runner.max_turns → default。"""
    resp: EffectiveResponse = manager.read_effective()

    # yaml 显式写的字段
    assert resp.values["model.preset_id"] == "local-gemma-4-e4b-it"
    assert resp.sources["model.preset_id"] == "yaml"
    assert resp.values["model.reasoning_effort"] == "medium"
    assert resp.sources["model.reasoning_effort"] == "yaml"

    # yaml 未写、走 pydantic 默认值的字段
    assert "runner.max_turns" in resp.values
    # RunnerConfig.max_turns 默认 = 50（src/infrastructure/config/models.py:403）
    assert resp.values["runner.max_turns"] == 50
    assert resp.sources["runner.max_turns"] == "default"

    # env_overrides 应为空（autouse fixture 清掉了所有 KONGMING_*）
    assert resp.env_overrides == []


def test_read_effective_evolution_reviewer_preset_defaults_to_inherit(
    manager: ConfigManager,
) -> None:
    """reviewer 未显式配置 preset 时继承主模型。"""
    resp = manager.read_effective()

    assert resp.values["evolution.learning.preset_id"] is None
    assert resp.sources["evolution.learning.preset_id"] == "default"


def test_read_effective_auto_trigger_default_and_schema(manager: ConfigManager) -> None:
    """cadence 自动触发默认开启，并通过 Web 配置 schema 暴露为可编辑布尔项。"""
    resp = manager.read_effective()
    meta = _schema.get_field_meta("evolution.learning.auto_trigger_enabled")

    assert resp.values["evolution.learning.auto_trigger_enabled"] is True
    assert resp.sources["evolution.learning.auto_trigger_enabled"] == "default"
    assert meta is not None
    assert meta.type == "bool"
    assert meta.editable is True
    assert meta.restart_required is True


# ---------------------------------------------------------------------------
# 4) read_effective：env override
# ---------------------------------------------------------------------------


def test_read_effective_env_override(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KONGMING_RUNNER_MAX_TURNS=20 → values=20, sources=env, 进入 env_overrides。"""
    monkeypatch.setenv("KONGMING_RUNNER_MAX_TURNS", "20")
    resp: EffectiveResponse = manager.read_effective()
    assert resp.values["runner.max_turns"] == 20
    assert resp.sources["runner.max_turns"] == "env"
    assert "runner.max_turns" in resp.env_overrides


def test_read_effective_env_override_scheduler_extra(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduler.enabled 由 loader._apply_scheduler_env_overrides 注入，
    不在 _ENV_FIELD_PATHS 内——确认 manager 补齐了这组额外白名单。"""
    monkeypatch.setenv("KONGMING_SCHEDULER_ENABLED", "false")
    resp: EffectiveResponse = manager.read_effective()
    assert resp.values["scheduler.enabled"] is False
    assert resp.sources["scheduler.enabled"] == "env"
    assert "scheduler.enabled" in resp.env_overrides


def test_read_effective_env_override_evolution_reviewer_preset(
    manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reviewer preset 支持独立 env 覆盖，不影响主模型选择。"""
    monkeypatch.setenv("KONGMING_EVOLUTION_LEARNING_PRESET_ID", "bigmodel-glm5-1m")

    resp = manager.read_effective()

    assert resp.values["evolution.learning.preset_id"] == "bigmodel-glm5-1m"
    assert resp.sources["evolution.learning.preset_id"] == "env"
    assert "evolution.learning.preset_id" in resp.env_overrides
    assert resp.values["model.preset_id"] == "local-gemma-4-e4b-it"


def test_read_effective_env_override_auto_trigger(
    manager: ConfigManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto_trigger_enabled 支持 loader 环境变量 round-trip。"""
    monkeypatch.setenv("KONGMING_EVOLUTION_LEARNING_AUTO_TRIGGER_ENABLED", "false")

    resp = manager.read_effective()

    assert resp.values["evolution.learning.auto_trigger_enabled"] is False
    assert resp.sources["evolution.learning.auto_trigger_enabled"] == "env"
    assert "evolution.learning.auto_trigger_enabled" in resp.env_overrides


# ---------------------------------------------------------------------------
# 5) save_patch happy path
# ---------------------------------------------------------------------------


def test_save_patch_happy_path(manager: ConfigManager, yaml_file: Path) -> None:
    """改默认 effort：ok=True / mtime 推进 / yaml 内容改。"""
    old_mtime = yaml_file.stat().st_mtime
    # 等一小会儿让 mtime 必然推进（macOS APFS 是 ns，但 sleep 一下保险）
    time.sleep(0.01)

    patch = [PatchItem(path="model.reasoning_effort", value="high")]
    resp: SavePatchResponse = manager.save_patch(patch, expected_mtime=old_mtime)

    assert resp.ok is True
    assert resp.new_mtime > old_mtime
    # 真实文件内容已改
    assert "reasoning_effort: high" in yaml_file.read_text(encoding="utf-8")
    # model.reasoning_effort 下次运行生效，无需重启 Web。
    assert resp.restart_required_fields == []


def test_save_patch_restart_required_fields(manager: ConfigManager, yaml_file: Path) -> None:
    """改 evolution.memory.enabled（restart_required=True）→ 出现在 restart_required_fields。

    先给 yaml 注入 evolution.memory.enabled = true 让 path 存在（writer 不补结构）。
    """
    extended_yaml = _MINI_YAML + ("\nevolution:\n  memory:\n    enabled: true\n")
    yaml_file.write_text(extended_yaml, encoding="utf-8")
    mtime = yaml_file.stat().st_mtime
    time.sleep(0.01)

    patch = [PatchItem(path="evolution.memory.enabled", value=False)]
    resp = manager.save_patch(patch, expected_mtime=mtime)
    assert resp.ok is True
    assert "evolution.memory.enabled" in resp.restart_required_fields


def test_save_patch_auto_trigger_round_trip(manager: ConfigManager, yaml_file: Path) -> None:
    """Web 配置 patch 可保存 cadence 开关，并由 loader 读回布尔值。"""
    extended_yaml = _MINI_YAML + ("\nevolution:\n  learning:\n    auto_trigger_enabled: true\n")
    yaml_file.write_text(extended_yaml, encoding="utf-8")
    mtime = yaml_file.stat().st_mtime

    resp = manager.save_patch(
        [PatchItem(path="evolution.learning.auto_trigger_enabled", value=False)],
        expected_mtime=mtime,
    )

    assert resp.ok is True
    assert "evolution.learning.auto_trigger_enabled" in resp.restart_required_fields
    assert manager.read_effective().values["evolution.learning.auto_trigger_enabled"] is False


@pytest.mark.parametrize("invalid_value", ["sometimes", []])
def test_save_patch_auto_trigger_rejects_invalid_values(
    manager: ConfigManager,
    yaml_file: Path,
    invalid_value: object,
) -> None:
    """auto_trigger_enabled 拒绝未知布尔文本和错误容器类型。"""
    extended_yaml = _MINI_YAML + ("\nevolution:\n  learning:\n    auto_trigger_enabled: true\n")
    yaml_file.write_text(extended_yaml, encoding="utf-8")
    mtime = yaml_file.stat().st_mtime

    with pytest.raises(ValidationFailedError):
        manager.save_patch(
            [
                PatchItem(
                    path="evolution.learning.auto_trigger_enabled",
                    value=invalid_value,
                )
            ],
            expected_mtime=mtime,
        )


# ---------------------------------------------------------------------------
# 6) save_patch validation 失败
# ---------------------------------------------------------------------------


def test_save_patch_validation_failed(manager: ConfigManager, yaml_file: Path) -> None:
    """非法 effort → raise ValidationFailedError。原文件保持原样。"""
    mtime = yaml_file.stat().st_mtime
    original = yaml_file.read_text(encoding="utf-8")

    patch = [PatchItem(path="model.reasoning_effort", value="ultra")]
    with pytest.raises(ValidationFailedError) as exc_info:
        manager.save_patch(patch, expected_mtime=mtime)

    # errors 列表非空
    assert exc_info.value.errors, "ValidationFailedError 应携带至少一条错误"
    # 原文件不变（writer 走 tmp + rename，校验失败 → tmp 被删，原文件原样）
    assert yaml_file.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# 7) save_patch mtime 冲突
# ---------------------------------------------------------------------------


def test_save_patch_conflict(manager: ConfigManager, yaml_file: Path) -> None:
    """先读 mtime，再外部触一下文件，再 save_patch 用旧 mtime → ConflictError。"""
    old_mtime = yaml_file.stat().st_mtime
    time.sleep(0.01)
    # 外部触发 mtime 推进（模拟另一个客户端写盘）
    yaml_file.write_text(yaml_file.read_text(encoding="utf-8"), encoding="utf-8")
    # 确认 mtime 真的变了
    assert yaml_file.stat().st_mtime > old_mtime

    patch = [PatchItem(path="model.reasoning_effort", value="low")]
    with pytest.raises(ConflictError):
        manager.save_patch(patch, expected_mtime=old_mtime)
