#!/usr/bin/env python3
"""sync-feature.py — 把私有功能分支同步到公开 PR（一功能一 PR）

输入: 私有功能分支名（如 ``feat/web-image-paste``）
行为:
    1. 在公开工作树基于 main 创建 ``dev-<feature-safe>`` 分支
    2. 用 ``git apply`` 增量应用每个提交的补丁（仅白名单内文件）
       — 保留 main 上后续提交的修复
    3. 每个提交重放后自动 ``ruff format`` + ``ruff check --fix``
    4. 推送 + 创建草稿 PR

设计要点（vs 旧的 sync-group.py）:
    - 增量应用差异（git apply --3way），减少整文件覆盖
    - PR 源分支 = dev-<feature-safe>，PR 目标分支 = main，一功能一 PR
    - 直接用 git 分支表达分组，省去 yaml 配置
    - 自动完成 ruff 格式化，减少持续集成失败后的手工补丁

用法:
    bash scripts/sync-feature.py feat/web-image-paste
    bash scripts/sync-feature.py feat/web-image-paste --no-pr  # 只推送，不开 PR
    bash scripts/sync-feature.py feat/web-image-paste --base origin/private-main  # 自定义基准分支
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PRIVATE = Path("/Volumes/machub_app/proj/kongming-agent")
PUBLIC = Path("/Volumes/machub_app/proj/kongming-agent-public")
INCLUDE_FILE = PRIVATE / ".publish" / "include.txt"
PUBLIC_REMOTE = "public"


def sh(cmd, cwd=None, check=True, env=None, input_data=None):
    full_env = None
    if env is not None:
        full_env = {**os.environ, **env}
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
        input=input_data,
    )
    if check and r.returncode != 0:
        print(f"❌ FAILED: {' '.join(cmd)}", file=sys.stderr)
        print(f"   stderr: {r.stderr[:500]}", file=sys.stderr)
        sys.exit(1)
    return r.stdout


def load_whitelist() -> list[str]:
    text = INCLUDE_FILE.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.split("\n") if ln.strip() and not ln.strip().startswith("#")]


def match_whitelist(path: str, whitelist: list[str]) -> bool:
    for entry in whitelist:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "feature_branch",
        help="私有 feature branch 名，如 feat/web-image-paste",
    )
    ap.add_argument(
        "--base",
        default="private-main",
        help="私有 base branch (默认 private-main)，用于算 base..feature 的 commits 范围。"
        "注意：本仓库 private-main 领先 main 600+ commit，base 必须是 private-main，"
        "用 main 会把整段历史都算进来",
    )
    ap.add_argument("--no-push", action="store_true", help="只 cherry-pick，不 push 远端")
    ap.add_argument("--no-pr", action="store_true", help="push 但不开 PR")
    args = ap.parse_args()

    whitelist = load_whitelist()
    feat = args.feature_branch
    feat_safe = feat.replace("/", "-").replace("_", "-")
    dev_branch = f"dev-{feat_safe}"

    # 验证私有功能分支存在
    check_branch = subprocess.run(
        ["git", "-C", str(PRIVATE), "rev-parse", "--verify", feat],
        capture_output=True,
        text=True,
    )
    if check_branch.returncode != 0:
        print(f"❌ 私有分支 '{feat}' 不存在")
        print(f"   提示：先 `git checkout -b {feat}` 在私有开发新功能")
        sys.exit(1)

    # 收集基准分支到功能分支之间的提交
    commits_text = sh(["git", "-C", str(PRIVATE), "rev-list", "--reverse", f"{args.base}..{feat}"])
    commits = [c for c in commits_text.strip().split("\n") if c]
    if not commits:
        print(f"⚠ '{feat}' 相对 '{args.base}' 没有新 commit")
        sys.exit(0)

    print(f"📦 feature: {feat}")
    print(f"   base: {args.base}")
    print(f"   target dev branch: {dev_branch}")
    print(f"   commits to apply: {len(commits)}")
    print()

    # ─── 1. 公开工作树切到干净 main ───────────────────────────
    print("─" * 70)
    print("[1/5] 切公开 worktree 到干净 main")
    sh(["git", "-C", str(PUBLIC), "checkout", "main"])
    sh(["git", "-C", str(PUBLIC), "fetch", PUBLIC_REMOTE, "main"])
    sh(["git", "-C", str(PUBLIC), "reset", "--hard", f"{PUBLIC_REMOTE}/main"])

    # ─── 2. 创建功能开发分支 ──────────────────────────────────
    print("─" * 70)
    print(f"[2/5] 创建 {dev_branch}（基于 main）")
    subprocess.run(
        ["git", "-C", str(PUBLIC), "branch", "-D", dev_branch],
        capture_output=True,
    )
    sh(["git", "-C", str(PUBLIC), "checkout", "-B", dev_branch, "main"])

    # ─── 3. 逐提交增量应用 ───────────────────────────────────
    print("─" * 70)
    print(f"[3/5] 增量 apply {len(commits)} 个 commit（仅白名单内文件）")
    synced = 0
    for i, h in enumerate(commits, 1):
        subject = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%s", h]).strip()
        short = h[:7]

        files_text = sh(
            ["git", "-C", str(PRIVATE), "diff-tree", "--no-commit-id", "--name-only", "-r", h]
        )
        all_files = [f for f in files_text.strip().split("\n") if f]
        whitelisted = [f for f in all_files if match_whitelist(f, whitelist)]

        if not whitelisted:
            print(f"   [{i}/{len(commits)}] {short} ⏭  无白名单内变更，跳过")
            continue

        # 生成补丁（仅白名单内文件）
        patch = sh(
            ["git", "-C", str(PRIVATE), "show", h, "--", *whitelisted],
            check=False,
        )
        if not patch.strip():
            print(f"   [{i}/{len(commits)}] {short} ⏭  patch 为空，跳过")
            continue

        # ★ 关键：用 git apply --3way 增量应用
        apply_result = subprocess.run(
            ["git", "-C", str(PUBLIC), "apply", "--3way", "--whitespace=nowarn"],
            capture_output=True,
            text=True,
            input=patch,
        )
        if apply_result.returncode != 0:
            # 应用失败 = 冲突 / 补丁不兼容
            stderr_short = apply_result.stderr[:500]
            print(f"   [{i}/{len(commits)}] {short} ❌ git apply 失败:")
            print(f"      {stderr_short}")
            print()
            print("   请人工解决冲突后重新跑：")
            print(f"     cd {PUBLIC}")
            print(f"     # 手工修复冲突文件 / git add / git commit -C {h}")
            sys.exit(1)

        # 增量格式化（仅修过的 .py 文件）
        py_files = [f for f in whitelisted if f.endswith(".py")]
        if py_files:
            subprocess.run(
                ["uv", "run", "--active", "ruff", "format", *py_files],
                cwd=PUBLIC,
                capture_output=True,
            )
            subprocess.run(
                ["uv", "run", "--active", "ruff", "check", "--fix", "--unsafe-fixes", *py_files],
                cwd=PUBLIC,
                capture_output=True,
            )

        # 添加文件 + 检查暂存区
        sh(["git", "-C", str(PUBLIC), "add", *whitelisted])
        check = subprocess.run(
            ["git", "-C", str(PUBLIC), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if check.returncode == 0:
            print(f"   [{i}/{len(commits)}] {short} ⏭  无 staging 变化，跳过")
            continue

        # 提交（保留原作者 / 日期 / 消息）
        orig_author = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%an <%ae>", h]).strip()
        orig_date = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%ai", h]).strip()
        orig_msg = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%B", h]).rstrip("\n")

        sh(
            [
                "git",
                "-C",
                str(PUBLIC),
                "commit",
                "--no-verify",
                f"--author={orig_author}",
                f"--date={orig_date}",
                "-m",
                orig_msg,
            ],
            env={"SKIP": "check-commit-separation"},
        )
        synced += 1
        print(f"   [{i}/{len(commits)}] {short} ✓  {subject[:90]}")

    if synced == 0:
        print("\n⚠️  无 commit 真正同步（全部跳过），中止")
        sys.exit(0)

    print()
    print(f"✓ 同步了 {synced}/{len(commits)} 个 commit")

    if args.no_push:
        print(f"\n--no-push 模式，本地 {dev_branch} 已就绪")
        return

    # ─── 4. 推送 ──────────────────────────────────────────────
    print("─" * 70)
    print(f"[4/5] push public/{dev_branch}")
    sh(
        ["git", "-C", str(PUBLIC), "push", "-f", PUBLIC_REMOTE, dev_branch],
        env={"SKIP": "pytest-picked,mypy,lint-imports"},
    )

    if args.no_pr:
        print("\n--no-pr 模式，push 完成不开 PR")
        return

    # ─── 5. 创建草稿 PR ───────────────────────────────────────
    print("─" * 70)
    print("[5/5] 开 draft PR")
    repo_url = sh(["git", "-C", str(PRIVATE), "remote", "get-url", PUBLIC_REMOTE]).strip()
    repo = re.sub(r".*github\.com[:/]", "", repo_url).rstrip("/").removesuffix(".git")

    pr_title = f"{feat}: {synced} commits"
    commit_lines = []
    for h in commits:
        subj = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%s", h]).strip()
        commit_lines.append(f"- {h[:7]} {subj}")
    body = f"""## Feature
`{feat}`

## Commits ({synced})
{chr(10).join(commit_lines)}
"""

    pr_url = sh(
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            dev_branch,
            "--title",
            pr_title,
            "--body",
            body,
            "--draft",
            "--repo",
            repo,
        ]
    ).strip()

    print(f"\n✅ PR 创建: {pr_url}")


if __name__ == "__main__":
    main()
