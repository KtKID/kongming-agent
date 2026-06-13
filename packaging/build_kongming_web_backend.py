"""构建 Kongming Web 后端 sidecar 的命令行脚本。

功能：
    读取 `packaging/kongming-web-backend.build.yaml`，校验构建输入，然后调用
    PyInstaller spec 产出 `kongming-web-backend.exe`。

作用：
    把 x-space sidecar 打包所需的构建配置独立放在 `packaging/`，避免混入
    运行时业务配置 `config/setting.yaml`。

关键执行流程：
    1. 读取并解析 build yaml。
    2. 校验入口脚本、数据目录和 PyInstaller spec 是否存在。
    3. 调用 `python -m PyInstaller ...`。
    4. 返回 PyInstaller 的退出码。

关键函数：
    `load_build_config`：读取 YAML，输出构建配置 dict。
    `validate_build_inputs`：校验构建输入，输出 spec 路径。
    `build_command`：生成 PyInstaller 命令。
    `main`：脚本入口，输出进程退出码。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "packaging" / "kongming-web-backend.build.yaml"


def load_build_config(path: Path) -> dict[str, Any]:
    """读取 build yaml。

    Args:
        path: build yaml 路径。

    Returns:
        YAML 解析后的 dict。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"build config root must be a mapping: {path}")
    return raw


def validate_build_inputs(config: dict[str, Any]) -> Path:
    """校验 PyInstaller 构建输入。

    Args:
        config: build yaml 解析结果。

    Returns:
        PyInstaller spec 的绝对路径。
    """
    pyinstaller_cfg = config.get("pyinstaller")
    entrypoint_cfg = config.get("entrypoint")
    if not isinstance(pyinstaller_cfg, dict) or not isinstance(entrypoint_cfg, dict):
        raise ValueError("build config must include pyinstaller and entrypoint sections")

    entry_script = REPO_ROOT / str(entrypoint_cfg.get("script", ""))
    if not entry_script.is_file():
        raise FileNotFoundError(f"entry script not found: {entry_script}")

    spec_path = REPO_ROOT / str(pyinstaller_cfg.get("spec", ""))
    if not spec_path.is_file():
        raise FileNotFoundError(f"PyInstaller spec not found: {spec_path}")

    for item in pyinstaller_cfg.get("data_files", []):
        if not isinstance(item, dict):
            raise ValueError(f"data_files item must be a mapping: {item!r}")
        source = REPO_ROOT / str(item.get("source", ""))
        if not source.exists():
            raise FileNotFoundError(f"data file source not found: {source}")

    return spec_path


def build_command(config: dict[str, Any], spec_path: Path) -> list[str]:
    """生成 PyInstaller 命令。

    Args:
        config: build yaml 解析结果。
        spec_path: PyInstaller spec 路径。

    Returns:
        可传给 `subprocess.run` 的命令数组。
    """
    pyinstaller_cfg = config["pyinstaller"]
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(REPO_ROOT / str(pyinstaller_cfg["dist_path"])),
        "--workpath",
        str(REPO_ROOT / str(pyinstaller_cfg["work_path"])),
        str(spec_path),
    ]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析构建脚本参数。

    Args:
        argv: 命令行参数；为 None 时读取系统参数。

    Returns:
        argparse 解析结果。
    """
    parser = argparse.ArgumentParser(prog="build-kongming-web-backend")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="独立 build yaml 路径。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行 sidecar 构建。

    Args:
        argv: 命令行参数；为 None 时读取系统参数。

    Returns:
        PyInstaller 子进程退出码。
    """
    args = _parse_args(argv)
    config_path = args.config.expanduser().resolve()
    config = load_build_config(config_path)
    spec_path = validate_build_inputs(config)
    command = build_command(config, spec_path)
    env = os.environ.copy()
    env["KONGMING_WEB_BACKEND_BUILD_CONFIG"] = str(config_path)
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())
