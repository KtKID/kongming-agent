"""XSpace runtime 配置契约测试。"""

from __future__ import annotations

import os
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from infrastructure.config import load_config
from infrastructure.config.models import Config
from infrastructure.config.paths import resolve_kongming_path

REPO_ROOT = Path(__file__).resolve().parents[3]
XSPACE_CONFIG = REPO_ROOT / "config" / "xspace" / "setting.yaml"


@pytest.fixture(autouse=True)
def _clean_kongming_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理会覆盖 x-space 资源配置默认值的 Kongming env。"""
    for name in tuple(os.environ):
        if name.startswith("KONGMING_"):
            monkeypatch.delenv(name, raising=False)


def _unwrap_model_type(annotation: object) -> type[BaseModel] | None:
    """从字段 annotation 中解析嵌套 BaseModel 类型。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (UnionType,):
        for arg in get_args(annotation):
            nested = _unwrap_model_type(arg)
            if nested is not None:
                return nested
    return None


def _config_leaf_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    """递归展开 Config leaf 字段路径。"""
    result: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        nested_model = _unwrap_model_type(field.annotation)
        if nested_model is not None:
            result.update(_config_leaf_paths(nested_model, path))
        else:
            result.add(path)
    return result


def _has_yaml_path(data: dict[str, Any], path: str) -> bool:
    """检查 YAML 是否显式声明指定字段路径。"""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def test_xspace_runtime_config_loads_with_product_defaults() -> None:
    """x-space 产品配置必须能通过正式 Config 校验。"""
    cfg = load_config(XSPACE_CONFIG, load_env_file=False)

    assert cfg.config_schema_version == "v0.5"
    assert cfg.web.enabled is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 60000
    assert cfg.web.host_environment == "browser"
    assert cfg.web.dev_mode is False
    assert cfg.session.backend == "file"
    assert cfg.scheduler.default_timezone == "Asia/Shanghai"
    assert cfg.model.api_key == ""
    assert cfg.web.initial_password is None


def test_xspace_runtime_env_marks_xspace_host(monkeypatch) -> None:
    """XSpace 运行态由启动 env 标记，覆盖持久配置默认值。"""
    monkeypatch.setenv("KONGMING_WEB_HOST_ENVIRONMENT", "xspace")

    cfg = load_config(XSPACE_CONFIG, load_env_file=False)

    assert cfg.web.host_environment == "xspace"


def test_xspace_runtime_config_declares_every_config_leaf_field() -> None:
    """新增 Config 字段时，x-space 配置必须同步显式分类维护。"""
    raw = yaml.safe_load(XSPACE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)

    missing = sorted(path for path in _config_leaf_paths(Config) if not _has_yaml_path(raw, path))
    assert missing == []


def test_xspace_runtime_data_paths_resolve_under_kongming_home(tmp_path: Path) -> None:
    """x-space 资源配置里的运行数据路径必须统一归入 kongming_home。"""
    cfg = load_config(XSPACE_CONFIG, load_env_file=False)
    home = tmp_path / ".kongming"

    assert (
        resolve_kongming_path(cfg.session.store_path, kongming_home=home)
        == (home / "sessions.db").resolve()
    )
    assert (
        resolve_kongming_path(cfg.session.file_store_path, kongming_home=home)
        == (home / "sessions").resolve()
    )
    assert (
        resolve_kongming_path(cfg.trace.output_path, kongming_home=home)
        == (home / "trace.jsonl").resolve()
    )
    assert (
        resolve_kongming_path(cfg.evolution.memory.root_path, kongming_home=home)
        == (home / "memory").resolve()
    )
    assert (
        resolve_kongming_path(cfg.evolution.learning.root_path, kongming_home=home)
        == (home / "evolution").resolve()
    )
    assert (
        resolve_kongming_path(cfg.web.full_log.path, kongming_home=home)
        == (home / "logs" / "full_log.jsonl").resolve()
    )
    assert cfg.scheduler.home is None
    assert cfg.workflow.home is None
    assert cfg.sitian.output_subdir is None
