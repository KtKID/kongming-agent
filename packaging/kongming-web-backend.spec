# -*- mode: python ; coding: utf-8 -*-
#
# Kongming Web 后端 sidecar 的 PyInstaller spec。
#
# 功能：
#   根据 `packaging/kongming-web-backend.build.yaml` 生成 Windows
#   `kongming-web-backend.exe`。
#
# 作用：
#   把隐藏导入、数据文件和输出路径集中到独立 build 配置，保持
#   `config/setting.yaml` 只承担运行时业务配置。
#
# 关键执行流程：
#   1. 读取 build yaml。
#   2. 把 hidden_imports / data_files 转换为 PyInstaller Analysis 参数。
#   3. 以 `packaging/kongming_web_backend_entry.py` 为入口生成 onefile exe。

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml
from PyInstaller.utils.hooks import collect_submodules


block_cipher = None
REPO_ROOT = Path.cwd()
DEFAULT_BUILD_CONFIG = REPO_ROOT / "packaging" / "kongming-web-backend.build.yaml"


def _parse_spec_args() -> Path:
    """解析 spec 透传参数。

    Returns:
        build yaml 的绝对路径。
    """
    env_path = os.environ.get("KONGMING_WEB_BACKEND_BUILD_CONFIG")
    if env_path and env_path.strip():
        return Path(env_path).expanduser().resolve()
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-config", type=Path, default=DEFAULT_BUILD_CONFIG)
    args, _unknown = parser.parse_known_args()
    return args.build_config.expanduser().resolve()


def _load_config(path: Path) -> dict:
    """读取 build yaml。

    Args:
        path: build yaml 路径。

    Returns:
        YAML 解析结果。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"build config root must be a mapping: {path}")
    return raw


def _data_files(config: dict) -> list[tuple[str, str]]:
    """转换 PyInstaller 数据文件配置。

    Args:
        config: build yaml 解析结果。

    Returns:
        `(source, target)` tuple 列表。
    """
    result = []
    for item in config["pyinstaller"].get("data_files", []):
        source = REPO_ROOT / item["source"]
        target = item["target"]
        result.append((str(source), target))
    return result


def _hidden_imports(config: dict) -> list[str]:
    """展开 PyInstaller hidden imports。

    Args:
        config: build yaml 解析结果。

    Returns:
        hidden import 模块名列表。
    """
    imports = []
    for name in config["pyinstaller"].get("hidden_imports", []):
        imports.append(name)
        imports.extend(collect_submodules(name))
    return sorted(set(imports))


build_config_path = _parse_spec_args()
build_config = _load_config(build_config_path)
entry_script = REPO_ROOT / build_config["entrypoint"]["script"]
artifact_name = build_config["artifact"]["name"]

a = Analysis(
    [str(entry_script)],
    pathex=[str(REPO_ROOT / "src"), str(REPO_ROOT)],
    binaries=[],
    datas=_data_files(build_config),
    hiddenimports=_hidden_imports(build_config),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=artifact_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
