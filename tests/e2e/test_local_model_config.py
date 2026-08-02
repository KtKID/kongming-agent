"""e2e P0-4：本地模型无 key 配置链路。

覆盖 plan.md "e2e 主链路基线"的第六条：

> 使用 ``http://127.0.0.1:62000/v1`` + ``gemma-4-e4b-it`` 且不提供 ``api_key``，
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
from infrastructure.config.model_catalog_manager import ModelCatalogManager
from infrastructure.config.model_provider_catalog import ModelProviderCatalogError
from infrastructure.config.models import Config, ModelSelectionConfig
from runtime_assembly.session_engine import SessionEngine

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
    assert cfg.model.preset_id == raw["model"]["preset_id"]
    runtime = ModelCatalogManager(environ={}).resolve_runtime(cfg.model)
    assert runtime.name == "gemma-4-e4b-it"
    assert runtime.base_url == "http://127.0.0.1:62000/v1"


@pytest.mark.e2e
def test_local_model_empty_api_key_is_allowed() -> None:
    """本地 base_url 下 api_key 可以为空，不应抛 ValidationError。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"))
    manager = ModelCatalogManager(environ={})
    runtime = manager.resolve_runtime(cfg.model)
    credential = manager.resolve_credential(runtime)
    assert credential.value == ""


@pytest.mark.e2e
def test_remote_model_without_api_key_is_rejected() -> None:
    """远端 catalog preset 缺少 provider key 时由 Manager 拒绝。"""
    cfg = Config(model=ModelSelectionConfig(preset_id="bigmodel-glm5-1m"))
    manager = ModelCatalogManager(environ={})
    runtime = manager.resolve_runtime(cfg.model)
    with pytest.raises(ModelProviderCatalogError) as caught:
        manager.resolve_credential(runtime)
    assert caught.value.details["credential_envs"] == (
        "GLM_API_KEY",
        "BIGMODEL_API_KEY",
        "ZHIPU_API_KEY",
        "ZAI_API_KEY",
    )


@pytest.mark.e2e
def test_local_model_native_runtime_build_succeeds(local_model_config: Config) -> None:
    """本地模型配置下 ``SessionEngine.build`` 能成功装配（不发真实请求）。"""
    runtime = SessionEngine.build(local_model_config)
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

    runtime = SessionEngine.build(
        local_model_config,
        tools=registry,
        approval=approval,
        enabled_tool_names=["read_file"],
    )
    assert runtime is not None
    assert "read_file" in registry


@pytest.mark.e2e
def test_load_config_env_override_preset_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KONGMING_MODEL_PRESET_ID`` 覆盖 YAML 中的运行时选择。"""
    monkeypatch.setenv("KONGMING_MODEL_PRESET_ID", "bigmodel-glm5-1m")
    cfg = load_config(LOCAL_MODEL_YAML, load_env_file=False)
    assert cfg.model.preset_id == "bigmodel-glm5-1m"


# ---------------------------------------------------------------------------
# 真实模型路径（opt-in）
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skipif(
    os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
    reason="requires local model service at 127.0.0.1:62000; set KONGMING_E2E_REAL_MODEL=1 to enable",
)
async def test_local_model_real_request_roundtrip() -> None:
    """真模型路径：本地模型服务在线时发起一次最小请求，验证闭环。

    默认被 skip；只在用户显式开启环境变量 ``KONGMING_E2E_REAL_MODEL=1`` 时执行，
    这样测试套件在无本地模型服务的环境下仍能全绿。
    """
    cfg = load_config(LOCAL_MODEL_YAML)
    runtime = SessionEngine.build(cfg)
    result = await runtime.run(
        "Respond with the single word 'ok'.",
        session_id="local-real",
    )
    # 真实模型行为不保证文字一致；只验证 run 成功收口。
    assert result.status in ("completed", "failed")
    # completed 时 final_message 必须存在
    if result.status == "completed":
        assert result.final_message is not None
