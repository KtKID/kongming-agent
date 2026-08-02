"""Harness Eval sandbox / pytest / git 辅助。

# 提供 eval 用的隔离文件写入、pytest 子进程执行、git 操作、
# diff 路径安全校验、symlink 逃逸检测等基础设施函数。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any


def write_file(root: Path, relative_path: str, content: str) -> Path:
    """在 sandbox 内写文件，输入根目录、相对路径和内容，输出绝对路径。"""

    target = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError(f"path escapes sandbox: {relative_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _pytest_env(sandbox_dir: Path) -> dict[str, str]:
    """构造 pytest 子进程环境，输入 sandbox 目录，输出收窄后的环境变量。"""

    keep_keys = {
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
    }
    env = {
        key: value for key, value in os.environ.items() if key in keep_keys or key.startswith("LC_")
    }
    home_dir = sandbox_dir / ".home"
    temp_dir = sandbox_dir / ".tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(temp_dir)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["PYTHONPATH"] = str(sandbox_dir)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# eval 专用 pytest 配置：阻断向上搜索项目根 pyproject.toml，
# 确保 eval 子进程不继承宿主的 asyncio_mode / testpaths / addopts 等配置。
_EVAL_PYTEST_CONFIG = """\
[tool.pytest.ini_options]
minversion = "8.0"
"""


def _ensure_eval_pytest_config(sandbox_dir: Path) -> None:
    """在工作目录写入 eval 专用 pyproject.toml，输入工作目录，无返回值。"""

    cfg = sandbox_dir / "pyproject.toml"
    if not cfg.exists():
        cfg.write_text(_EVAL_PYTEST_CONFIG, encoding="utf-8")


def run_pytest(sandbox_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    """在 sandbox 内运行 pytest，输入目录和超时，输出进程结果。"""

    _ensure_eval_pytest_config(sandbox_dir)
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(sandbox_dir),
            "--noconftest",
            "-p",
            "no:cacheprovider",
        ],
        cwd=sandbox_dir,
        env=_pytest_env(sandbox_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": proc.stdout,
    }


def run_pytest_nodes(
    sandbox_dir: Path, node_ids: list[str], timeout_seconds: int
) -> dict[str, Any]:
    """在 sandbox 内按 pytest node id 选择性运行测试，输入目录/节点列表/超时，输出进程结果。"""

    if not node_ids:
        return {
            "exit_code": 2,
            "duration_ms": 0,
            "output": "no pytest nodes configured",
            "skipped": True,
        }
    _ensure_eval_pytest_config(sandbox_dir)
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(sandbox_dir),
            "--noconftest",
            "-p",
            "no:cacheprovider",
            *node_ids,
        ],
        cwd=sandbox_dir,
        env=_pytest_env(sandbox_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": proc.stdout,
    }


def run_git(
    args: list[str], cwd: Path, *, stdin: str | None = None, timeout_seconds: int = 30
) -> subprocess.CompletedProcess[str]:
    """在指定目录执行一次 git 命令，输入参数列表/工作目录/可选 stdin，输出进程结果。

    通过命令级 `-c user.*` 提供提交身份，绝不写入任何持久 git config。
    """

    base = [
        "git",
        "-c",
        "user.email=eval@kongming.local",
        "-c",
        "user.name=kongming-eval",
        "-c",
        "commit.gpgsign=false",
    ]
    command = base + args
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=f"git command timed out after {timeout_seconds}s: {exc}",
        )


def init_repo_with_base(repo_dir: Path, base_files: dict[str, Any]) -> None:
    """初始化 SWE-bench 风格 base 仓库，输入仓库目录和 base 文件映射，无返回值。"""

    repo_dir.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], repo_dir)
    for relative_path, content in base_files.items():
        write_file(repo_dir, str(relative_path), str(content))
    run_git(["add", "-A"], repo_dir)
    run_git(["commit", "-q", "-m", "base commit"], repo_dir)


def _diff_path_token(line: str) -> list[str]:
    """从 diff header 行提取路径 token，输入一行 patch，输出路径列表。"""

    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    if len(parts) >= 4 and parts[0] == "diff" and parts[1] == "--git":
        return [parts[2], parts[3]]
    if len(parts) >= 2 and parts[0] in {"---", "+++"}:
        return [parts[1]]
    return []


def _normalize_diff_path_token(token: str) -> str | None:
    """规范化 diff path token，输入原始 token，输出仓库相对路径或 None。"""

    if token == "/dev/null":
        return None
    if token.startswith(("a/", "b/")):
        token = token[2:]
    return token


def validate_diff_paths(repo_dir: Path, diff_text: str) -> str | None:
    """校验 diff 路径是否留在 repo 内，输入 repo 和 patch，输出错误字符串或 None。"""

    root = repo_dir.resolve()
    for line in diff_text.splitlines():
        if line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            return f"unsupported diff metadata: {line}"
        if line.startswith(("GIT binary patch", "Binary files ")):
            return f"unsupported binary diff: {line}"
        if not (
            line.startswith("diff --git ") or line.startswith("--- ") or line.startswith("+++ ")
        ):
            continue
        tokens = _diff_path_token(line)
        if not tokens:
            return f"invalid diff header: {line}"
        for token in tokens:
            normalized = _normalize_diff_path_token(token)
            if normalized is None:
                continue
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or not normalized:
                return f"path escapes sandbox: {token}"
            if path.parts and path.parts[0] == ".git":
                return f"git metadata path is unsupported: {token}"
            target = (root / Path(*path.parts)).resolve()
            if root not in target.parents and target != root:
                return f"path escapes sandbox: {token}"
    return None


def validate_repo_symlinks(repo_dir: Path) -> str | None:
    """校验 repo 内符号链接不逃出 sandbox，输入 repo，输出错误字符串或 None。"""

    root = repo_dir.resolve()
    for path in repo_dir.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.resolve(strict=False)
        if root not in target.parents and target != root:
            relative_path = path.relative_to(repo_dir)
            return f"symlink escapes sandbox: {relative_path} -> {target}"
    return None


def apply_model_diff(repo_dir: Path, diff_text: str) -> dict[str, Any]:
    """把模型产出的 diff 应用到 base 仓库，输入仓库目录和 patch 文本，输出应用结果。"""

    if not diff_text.strip():
        return {"applied": False, "strategy": None, "output": "empty diff"}
    path_error = validate_diff_paths(repo_dir, diff_text)
    if path_error:
        return {
            "applied": False,
            "strategy": "rejected-outside-sandbox",
            "output": path_error,
        }
    primary = run_git(["apply", "--whitespace=nowarn", "-"], repo_dir, stdin=diff_text)
    if primary.returncode == 0:
        symlink_error = validate_repo_symlinks(repo_dir)
        if symlink_error:
            return {
                "applied": False,
                "strategy": "rejected-outside-sandbox",
                "output": symlink_error,
            }
        return {"applied": True, "strategy": "git-apply", "output": primary.stdout}
    return {"applied": False, "strategy": None, "output": f"git-apply:\n{primary.stdout}"}
