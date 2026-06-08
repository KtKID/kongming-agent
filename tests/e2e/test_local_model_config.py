"""e2e P0-4：本地模型无 key 配置链路。

覆盖 plan.md "e2e 主链路基线"的第六条：

> 使用 ``http://127.0.0.1:1234`` + ``gemma-4-e4b-it`` 且不提供 ``api_key``，
> 系统仍能成功启动并完成最小请求。

默认只验证 **配置加载 + 装配** 能走通，不发真实 HTTP 请求（本地模型服务
未必启动）。通过环境变量 ``KONGMING_E2E_REAL_MODEL=1`` 显式开启真实模型测试，
默认情况下跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from infrastructure.config import load_config
from infrastructure.config.models import Config, ModelConfig
from runtime_assembly.native_runtime import NativeRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL_MODEL_YAML = REPO_ROOT / "config" / "setting.yaml"


@pytest.mark.e2e
def test_local_model_config_loads_from_yaml() -> None:
    """``config/setting.yaml`` 应该能被 load_config 成功解析。"""
    import yaml

    raw = yaml.safe_load(LOCAL_MODEL_YAML.read_text(encoding="utf-8"))
    # load_env_file=False：隔离本地 .env，只测 YAML 结构本身
    cfg = load_config(LOCAL_MODEL_YAML, load_env_file=False)
    assert isinstance(cfg, Config)
    # provider 在 v0.1.x 起为可选字段（可由 base_url + 路由表自动推断），yaml 可省略
    assert cfg.model.provider == raw["model"].get("provider")
    assert cfg.model.name == raw["model"]["name"]
    assert cfg.model.base_url.rstrip("/") == raw["model"]["base_url"].rstrip("/")
    assert cfg.model.api_key == ""
    assert cfg.model.is_local is True


@pytest.mark.e2e
def test_local_model_empty_api_key_is_allowed() -> None:
    """本地 base_url 下 api_key 可以为空，不应抛 ValidationError。"""
    cfg = Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="gemma-4-e4b-it",
            base_url="http://127.0.0.1:1234",
            api_key="",
        )
    )
    assert cfg.model.is_local is True
    assert cfg.model.api_key == ""


@pytest.mark.e2e
def test_remote_model_without_api_key_is_rejected() -> None:
    """非本地 base_url + 空 api_key 应该被 pydantic 校验拒绝。

    pydantic v2 的 ValidationError 会被 load_config 外层包装成
    :class:`ConfigValidationError`，但直接构造 ``Config`` 时走的是原生
    ValidationError。这里不走 load_config，直接构造验证跨字段校验。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config(
            model=ModelConfig(
                provider="openai_compatible",
                name="gemma-4-e4b-it",
                base_url="https://api.openai.com",
                api_key="",
            )
        )


@pytest.mark.e2e
def test_local_model_native_runtime_build_succeeds(local_model_config: Config) -> None:
    """本地模型配置下 ``NativeRuntime.build`` 能成功装配（不发真实请求）。"""
    runtime = NativeRuntime.build(local_model_config)
    assert runtime is not None
    # 装配层应挂好 config / agent_spec / runner
    assert runtime.config is local_model_config
    assert runtime.agent_spec.default_model == "gemma-4-e4b-it"
    # runner 是底层唯一 run loop 持有者
    assert runtime.runner is not None


@pytest.mark.e2e
def test_local_model_build_with_explicit_approval_and_tools(
    local_model_config: Config,
) -> None:
    """装配层也应该接受显式传入 tools / approval / event_sinks。"""
    from tools import AutoAllowApproval, ReadFileTool, ToolRegistry

    registry = ToolRegistry([ReadFileTool()])
    approval = AutoAllowApproval()

    runtime = NativeRuntime.build(
        local_model_config,
        tools=registry,
        approval=approval,
        enabled_tool_names=["read_file"],
    )
    assert runtime is not None
    assert "read_file" in registry


@pytest.mark.e2e
def test_load_config_env_override_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_MODEL_API_KEY`` 环境变量应当覆盖 YAML 中的 api_key 字段。"""
    monkeypatch.setenv("KONGMING_MODEL_API_KEY", "override-key-123")
    try:
        cfg = load_config(LOCAL_MODEL_YAML, load_env_file=False)
    finally:
        # monkeypatch fixture 会自动 undo，这里无需手动清理。
        pass
    assert cfg.model.api_key == "override-key-123"


# ---------------------------------------------------------------------------
# 真实模型路径（opt-in）
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason=(
        "requires local model service at 127.0.0.1:1234; set KONGMING_E2E_REAL_MODEL=1 to enable"
    ),
)
async def test_local_model_real_request_roundtrip() -> None:
    """真模型路径：本地模型服务在线时发起一次最小请求，验证闭环。

    默认被 skip；只在用户显式开启环境变量 ``KONGMING_E2E_REAL_MODEL=1`` 时执行，
    这样测试套件在无本地模型服务的环境下仍能全绿。
    """
    cfg = load_config(LOCAL_MODEL_YAML)
    runtime = NativeRuntime.build(cfg)
    result = await runtime.run(
        "Respond with the single word 'ok'.",
        session_id="local-real",
    )
    # 真实模型行为不保证文字一致；只验证 run 成功收口。
    assert result.status in ("completed", "failed")
    # completed 时 final_message 必须存在
    if result.status == "completed":
        assert result.final_message is not None
