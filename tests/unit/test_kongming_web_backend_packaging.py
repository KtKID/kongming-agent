"""Kongming Web 后端 sidecar 打包配置测试。"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

from infrastructure.config import load_config, resolve_kongming_path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "packaging" / "build_kongming_web_backend.py"

_spec = importlib.util.spec_from_file_location("kongming_web_backend_build", BUILD_SCRIPT)
assert _spec is not None and _spec.loader is not None
_build_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_build_module)
build_command = _build_module.build_command
load_build_config = _build_module.load_build_config
validate_build_inputs = _build_module.validate_build_inputs


def test_pyproject_includes_runtime_packages_and_data() -> None:
    """wheel 清单覆盖 Web 启动路径真实 import 的顶层包和模板数据。"""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    packages = set(wheel["packages"])

    assert "src/scheduler" in packages
    assert "src/evolution" in packages
    assert "src/network" in packages
    assert "src/commands" in packages
    assert "src/devtools" in packages
    assert wheel["force-include"]["src/prompts/templates"] == "prompts/templates"
    assert (
        wheel["force-include"]["src/safety/auto_approval/default_rules.yaml"]
        == "safety/auto_approval/default_rules.yaml"
    )
    assert wheel["force-include"]["config/xspace/setting.yaml"] == "config/setting.yaml"


def test_build_yaml_is_independent_from_runtime_setting() -> None:
    """build yaml 位于 packaging 下，且只描述构建期 artifact。"""
    path = REPO_ROOT / "packaging" / "kongming-web-backend.build.yaml"
    config = load_build_config(path)

    assert config["artifact"]["name"] == "kongming-web-backend"
    assert config["entrypoint"]["script"] == "packaging/kongming_web_backend_entry.py"
    assert config["pyinstaller"]["spec"] == "packaging/kongming-web-backend.spec"
    assert "web/dist" in {item["source"] for item in config["pyinstaller"]["data_files"]}
    assert "src/safety/auto_approval/default_rules.yaml" in {
        item["source"] for item in config["pyinstaller"]["data_files"]
    }
    assert {
        "source": "config/xspace/setting.yaml",
        "target": "config",
    } in config["pyinstaller"]["data_files"]
    assert "config/setting.yaml" not in str(path)


def test_build_command_uses_spec_and_dedicated_output_dirs() -> None:
    """build 脚本通过 spec 构建，并使用独立 dist/work 目录。"""
    config = load_build_config(REPO_ROOT / "packaging" / "kongming-web-backend.build.yaml")
    spec_path = REPO_ROOT / "packaging" / "kongming-web-backend.spec"
    command = build_command(config, spec_path)
    joined = " ".join(command)

    assert "PyInstaller" in joined
    assert str(spec_path) in command
    assert "--distpath" in command
    assert "--workpath" in command
    assert str(REPO_ROOT / "dist" / "kongming-web-backend") in joined


def test_build_inputs_exist_for_backend_compile() -> None:
    """backend sidecar 构建输入完整存在。"""
    config = load_build_config(REPO_ROOT / "packaging" / "kongming-web-backend.build.yaml")
    spec_path = validate_build_inputs(config)

    assert spec_path == REPO_ROOT / "packaging" / "kongming-web-backend.spec"


def test_xspace_setting_kongming_paths_resolve_under_home(tmp_path: Path) -> None:
    """xspace 运行配置中的 .kongming/* 字段按 kongming_home 派生。"""
    cfg = load_config(REPO_ROOT / "config" / "xspace" / "setting.yaml", load_env_file=False)
    home = tmp_path / "home"

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
