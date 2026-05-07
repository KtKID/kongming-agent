"""Workspace Git 只读模型。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from web.workspace import normalize_relative_path, resolve_workspace_path

WORKSPACE_GIT_LOG_LIMIT = 30


class WorkspaceGitError(ValueError):
    """workspace git 相关错误。"""


@dataclass(frozen=True)
class WorkspaceGitStatusEntry:
    path: str
    name: str
    staged_status: str
    unstaged_status: str
    previous_path: str | None = None


def require_git_repo(root: Path) -> Path:
    """要求 workspace root 位于 git 仓库内，并返回 repo root。"""
    result = _run_git(root, "rev-parse", "--show-toplevel")
    repo_root = Path(result.stdout.strip()).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        raise WorkspaceGitError(f"git repo root not found: {repo_root}")
    return repo_root


def read_git_status(root: Path) -> dict[str, object]:
    """读取当前 workspace 的 git 状态。"""
    repo_root = require_git_repo(root)
    result = _run_git(
        root,
        "status",
        "--porcelain=1",
        "--branch",
    )
    lines = [line.rstrip("\n") for line in result.stdout.splitlines()]
    header = lines[0] if lines else ""
    current_branch, tracking_branch, ahead_count, behind_count = _parse_status_header(header)
    changes = [_parse_status_entry(line) for line in lines[1:] if line.strip()]
    return {
        "workspace_root": str(root),
        "repo_root": str(repo_root),
        "current_branch": current_branch,
        "tracking_branch": tracking_branch,
        "ahead_count": ahead_count,
        "behind_count": behind_count,
        "changes": changes,
    }


def read_git_branches(root: Path) -> dict[str, object]:
    """列出本地与远端分支。"""
    require_git_repo(root)
    current_branch = _current_branch(root)
    local = [
        line.strip()
        for line in _run_git(
            root, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        ).stdout.splitlines()
        if line.strip()
    ]
    remote = [
        line.strip()
        for line in _run_git(
            root, "for-each-ref", "--format=%(refname:short)", "refs/remotes"
        ).stdout.splitlines()
        if line.strip() and not line.endswith("/HEAD")
    ]
    return {
        "current_branch": current_branch,
        "local_branches": local,
        "remote_branches": remote,
    }


def read_git_commits(root: Path, *, limit: int = WORKSPACE_GIT_LOG_LIMIT) -> dict[str, object]:
    """读取最近提交。"""
    require_git_repo(root)
    result = _run_git(
        root,
        "log",
        f"-n{limit}",
        "--date=iso-strict",
        "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s",
    )
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        full, short, author, authored_at, subject = line.split("\x1f", 4)
        commits.append(
            {
                "commit": full,
                "short_commit": short,
                "author": author,
                "authored_at": authored_at,
                "subject": subject,
            }
        )
    return {"commits": commits}


def read_git_file_diff(root: Path, relative_path: str) -> dict[str, object]:
    """读取单文件差异。"""
    require_git_repo(root)
    normalized = normalize_relative_path(relative_path)
    if not normalized:
        raise WorkspaceGitError("workspace git diff path is required")
    target = resolve_workspace_path(root, normalized)
    staged = _run_git(root, "diff", "--cached", "--", normalized).stdout
    unstaged = _run_git(root, "diff", "--", normalized).stdout
    pieces = [chunk for chunk in (staged.strip("\n"), unstaged.strip("\n")) if chunk]
    if not pieces and target.exists():
        no_index = _run_git(
            root,
            "diff",
            "--no-index",
            "--",
            "/dev/null",
            str(target),
            check=False,
            allowed_returncodes={0, 1},
        ).stdout.strip("\n")
        if no_index:
            pieces.append(no_index)
    return {
        "path": normalized,
        "diff": "\n\n".join(pieces).strip(),
    }


def stage_git_paths(root: Path, paths: list[str]) -> dict[str, object]:
    """把路径批量写入 git index。"""
    require_git_repo(root)
    normalized = _normalize_paths(paths)
    _run_git(root, "add", "-A", "--", *normalized)
    return {"detail": f"staged {len(normalized)} path(s)"}


def unstage_git_paths(root: Path, paths: list[str]) -> dict[str, object]:
    """把路径批量从 git index 撤出。"""
    require_git_repo(root)
    normalized = _normalize_paths(paths)
    _run_git(root, "restore", "--staged", "--", *normalized)
    return {"detail": f"unstaged {len(normalized)} path(s)"}


def checkout_git_branch(root: Path, branch: str) -> dict[str, object]:
    """切换到已有分支。"""
    require_git_repo(root)
    normalized = _normalize_branch_name(root, branch)
    _run_git(root, "checkout", normalized)
    return {
        "detail": f"checked out {normalized}",
        "current_branch": _current_branch(root),
    }


def create_git_branch(root: Path, branch: str, *, checkout: bool = True) -> dict[str, object]:
    """创建分支，可选立即切换。"""
    require_git_repo(root)
    normalized = _normalize_branch_name(root, branch)
    if checkout:
        _run_git(root, "checkout", "-b", normalized)
    else:
        _run_git(root, "branch", normalized)
    current_branch = _current_branch(root) if checkout else None
    return {
        "detail": f"created branch {normalized}",
        "current_branch": current_branch,
    }


def commit_git(root: Path, message: str) -> dict[str, object]:
    """基于当前 index 执行 git commit。"""
    require_git_repo(root)
    commit_message = message.strip()
    if not commit_message:
        raise WorkspaceGitError("git commit message is required")
    _run_git(root, "commit", "-m", commit_message)
    full_commit = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    short_commit = _run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return {
        "detail": f"committed {short_commit}",
        "commit": full_commit,
        "short_commit": short_commit,
        "current_branch": _current_branch(root),
    }


def _run_git(
    root: Path,
    *args: str,
    check: bool = True,
    allowed_returncodes: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    """执行 git CLI。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    allowed = allowed_returncodes if allowed_returncodes is not None else {0}
    if check and result.returncode not in allowed:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise WorkspaceGitError(message)
    return result


def _normalize_paths(paths: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in paths:
        path = normalize_relative_path(item)
        if not path:
            raise WorkspaceGitError("workspace git path is required")
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    if not normalized:
        raise WorkspaceGitError("workspace git paths are required")
    return normalized


def _normalize_branch_name(root: Path, branch: str) -> str:
    normalized = branch.strip()
    if not normalized:
        raise WorkspaceGitError("git branch name is required")
    _run_git(root, "check-ref-format", "--branch", normalized)
    return normalized


def _parse_status_header(header: str) -> tuple[str, str | None, int, int]:
    current_branch = _current_branch_from_header(header)
    tracking_branch: str | None = None
    ahead_count = 0
    behind_count = 0
    if "..." in header:
        after = header.split("...", 1)[1]
        tracking_branch = after.split(" ", 1)[0].split("[", 1)[0].strip() or None
    if "[" in header and "]" in header:
        counts = header.split("[", 1)[1].split("]", 1)[0]
        for part in counts.split(","):
            item = part.strip()
            if item.startswith("ahead "):
                ahead_count = int(item.split(" ", 1)[1] or "0")
            if item.startswith("behind "):
                behind_count = int(item.split(" ", 1)[1] or "0")
    return current_branch, tracking_branch, ahead_count, behind_count


def _current_branch_from_header(header: str) -> str:
    if not header.startswith("## "):
        return ""
    body = header[3:]
    branch = body.split("...", 1)[0].split(" ", 1)[0].strip()
    return "HEAD" if branch == "HEAD" else branch


def _current_branch(root: Path) -> str:
    result = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = result.stdout.strip()
    if branch:
        return branch
    detached = _run_git(root, "rev-parse", "--short", "HEAD")
    return detached.stdout.strip()


def _parse_status_entry(line: str) -> dict[str, object]:
    staged = line[0]
    unstaged = line[1]
    raw_path = line[3:]
    previous_path: str | None = None
    if " -> " in raw_path:
        old_path, new_path = raw_path.split(" -> ", 1)
        previous_path = normalize_relative_path(old_path)
        raw_path = new_path
    path = normalize_relative_path(raw_path)
    return {
        "path": path,
        "name": Path(path).name,
        "staged_status": staged,
        "unstaged_status": unstaged,
        "previous_path": previous_path,
    }


__all__ = [
    "WORKSPACE_GIT_LOG_LIMIT",
    "WorkspaceGitError",
    "WorkspaceGitStatusEntry",
    "checkout_git_branch",
    "commit_git",
    "create_git_branch",
    "read_git_branches",
    "read_git_commits",
    "read_git_file_diff",
    "read_git_status",
    "require_git_repo",
    "stage_git_paths",
    "unstage_git_paths",
]
