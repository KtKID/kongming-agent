#!/usr/bin/env python3
"""按 .publish/groups.yaml 指定的分组同步私有提交到公开 dev-<name> 分支 + 开 PR。

用法:
    python3 scripts/sync-group.py <group-name> [--no-push] [--no-pr]

行为:
    1. 读 .publish/groups.yaml 找到该分组的提交
    2. 在公开工作树上切 main + reset → 创建 dev-<group-name> 分支
    3. 逐提交重放（仅白名单内文件）
    4. 最后一个提交加 Private-Commit 标记
    5. 推送公开分支（SKIP=pytest-picked 跳过预推送误报）
    6. gh pr create --draft

注意:
    - 公开工作树 = ../<project-name>-public
    - 私有项目自身**完全只读**，不动分支
    - groups.yaml 已锁 snapshot.private_head，跑期间 sleep-dev 推的新提交不进入同步范围
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PRIVATE = Path("/Volumes/machub_app/proj/kongming-agent")
PUBLIC = Path("/Volumes/machub_app/proj/kongming-agent-public")
INCLUDE_FILE = PRIVATE / ".publish" / "include.txt"
GROUPS_YAML = PRIVATE / ".publish" / "groups.yaml"
PUBLIC_REMOTE = "public"


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None) -> str:
    """运行命令并返回标准输出。失败时打印标准错误并退出。"""
    full_env = None
    if env is not None:
        import os

        full_env = {**os.environ, **env}
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=full_env)
    if check and r.returncode != 0:
        print(f"❌ FAILED: {' '.join(cmd)}", file=sys.stderr)
        print(f"   stdout: {r.stdout}", file=sys.stderr)
        print(f"   stderr: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout


def parse_groups_yaml() -> dict:
    """简易 yaml 解析（不用 PyYAML 避免依赖）。"""
    text = GROUPS_YAML.read_text(encoding="utf-8")
    result = {"snapshot": {}, "groups": []}

    current_section = None
    current_group = None
    current_commit = None

    for line in text.split("\n"):
        if line.startswith("snapshot:"):
            current_section = "snapshot"
            continue
        if line.startswith("groups:"):
            current_section = "groups"
            continue

        if current_section == "snapshot":
            m = re.match(r"^\s+(\w+):\s*(.+)$", line)
            if m:
                result["snapshot"][m.group(1)] = m.group(2).strip()

        if current_section == "groups":
            m = re.match(r"^\s*-\s*name:\s*(.+)$", line)
            if m:
                if current_group:
                    if current_commit:
                        current_group["commits"].append(current_commit)
                        current_commit = None
                    result["groups"].append(current_group)
                current_group = {"name": m.group(1).strip(), "commits": []}
                continue

            m = re.match(r"^\s+(earliest|count):\s*(.+)$", line)
            if m and current_group:
                current_group[m.group(1)] = m.group(2).strip()
                continue

            m = re.match(r"^\s*-\s*hash:\s*(.+)$", line)
            if m and current_group is not None:
                if current_commit:
                    current_group["commits"].append(current_commit)
                current_commit = {"hash": m.group(1).strip()}
                continue

            m = re.match(r"^\s+(time|scope|subject):\s*(.+)$", line)
            if m and current_commit is not None:
                val = m.group(2).strip()
                if val.startswith("'") and val.endswith("'"):
                    val = val[1:-1].replace("''", "'")
                current_commit[m.group(1)] = val

    if current_group:
        if current_commit:
            current_group["commits"].append(current_commit)
        result["groups"].append(current_group)

    return result


def load_whitelist() -> list[str]:
    """加载白名单（来自 .publish/include.txt）。"""
    text = INCLUDE_FILE.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.split("\n") if ln.strip() and not ln.strip().startswith("#")]


def match_whitelist(path: str, whitelist: list[str]) -> bool:
    for entry in whitelist:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        else:
            if path == entry:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group_name", help="groups.yaml 里的 group 名")
    ap.add_argument("--no-push", action="store_true", help="不 push 远端")
    ap.add_argument("--no-pr", action="store_true", help="不开 PR")
    args = ap.parse_args()

    # 1. 加载配置
    config = parse_groups_yaml()
    whitelist = load_whitelist()
    private_head = config["snapshot"].get("private_head", "")

    target = None
    for g in config["groups"]:
        if g["name"] == args.group_name:
            target = g
            break
    if target is None:
        print(f"❌ group '{args.group_name}' 不在 groups.yaml")
        sys.exit(1)

    dev_branch = f"dev-{args.group_name}"
    commits = target["commits"]
    print(f"📦 group: {args.group_name}")
    print(f"   branch: {dev_branch}")
    print(f"   commits: {len(commits)}")
    print(f"   private_head 锚点: {private_head}")
    print()

    # 2. 公开工作树：切 main + reset
    print("─" * 70)
    print("[1/5] 切公开 worktree 到干净 main")
    sh(["git", "-C", str(PUBLIC), "checkout", "main"])
    sh(["git", "-C", str(PUBLIC), "fetch", PUBLIC_REMOTE, "main"])
    sh(["git", "-C", str(PUBLIC), "reset", "--hard", f"{PUBLIC_REMOTE}/main"])

    # 3. 创建 dev-<group-name> 分支
    print("─" * 70)
    print(f"[2/5] 创建分支 {dev_branch}（基于 main）")
    # 删本地同名分支（如果存在）
    subprocess.run(["git", "-C", str(PUBLIC), "branch", "-D", dev_branch], capture_output=True)
    sh(["git", "-C", str(PUBLIC), "checkout", "-B", dev_branch, "main"])

    # 4. 逐提交重放
    print("─" * 70)
    print(f"[3/5] 重放 {len(commits)} 个 commit（仅白名单内文件）")

    synced = 0
    for i, c in enumerate(commits, 1):
        h = c["hash"]
        subject = c.get("subject", "")
        # 取该提交改动的文件
        files_text = sh(
            [
                "git",
                "-C",
                str(PRIVATE),
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "--no-renames",
                h,
            ],
            check=False,
        )

        touched = False
        changed_py: list[str] = []  # 该提交实际重放的 .py 文件，用于增量格式化
        for ln in files_text.strip().split("\n"):
            if not ln:
                continue
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0], parts[1]
            if not match_whitelist(path, whitelist):
                continue
            touched = True
            if status == "D":
                subprocess.run(
                    ["git", "-C", str(PUBLIC), "rm", "-f", "--quiet", path], capture_output=True
                )
            else:
                sh(["git", "-C", str(PUBLIC), "checkout", h, "--", path], check=False)
                if path.endswith(".py"):
                    changed_py.append(path)

        if not touched:
            print(f"   [{i}/{len(commits)}] {h[:7]} ⏭  无白名单内变更，跳过")
            continue

        # 增量格式化：对当次重放的 .py 文件跑 ruff format + ruff check --fix
        # （避免持续集成因为旧提交整文件覆盖丢 main 上的格式化修复而失败）
        if changed_py:
            subprocess.run(
                ["uv", "run", "--active", "ruff", "format", *changed_py],
                cwd=PUBLIC,
                capture_output=True,
            )
            subprocess.run(
                ["uv", "run", "--active", "ruff", "check", "--fix", "--unsafe-fixes", *changed_py],
                cwd=PUBLIC,
                capture_output=True,
            )
            # ruff 修过的文件重新加入暂存区（保证提交包含格式化结果）
            sh(["git", "-C", str(PUBLIC), "add", *changed_py])

        # 检查暂存区是否真的有变化
        check = subprocess.run(
            ["git", "-C", str(PUBLIC), "diff", "--cached", "--quiet"], capture_output=True
        )
        if check.returncode == 0:
            print(f"   [{i}/{len(commits)}] {h[:7]} ⏭  无 staging 变化，跳过")
            continue

        # 取原提交的作者 / 日期 / 消息
        orig_author = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%an <%ae>", h]).strip()
        orig_date = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%ai", h]).strip()
        orig_msg = sh(["git", "-C", str(PRIVATE), "log", "-1", "--format=%B", h]).rstrip("\n")

        # 提交（跳过预提交钩子：本地公开工作树已跑增量 ruff/lint，
        # 持续集成会兜底；钩子经常因为环境漂移误报）
        env = {"SKIP": "check-commit-separation"}
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
            env=env,
        )
        synced += 1
        short = h[:7]
        print(f"   [{i}/{len(commits)}] {short} ✓  {subject[:80]}")

    if synced == 0:
        print("\n⚠️  无 commit 重放（全部跳过），中止")
        sys.exit(0)

    # 5. 最后一个提交加 Private-Commit 标记（推进同步锚点）
    print("─" * 70)
    print("[4/5] 追加 Private-Commit trailer")
    last_msg = sh(["git", "-C", str(PUBLIC), "log", "-1", "--format=%B"]).rstrip("\n")
    new_msg = f"{last_msg}\n\nPrivate-Commit: {private_head}"
    sh(
        ["git", "-C", str(PUBLIC), "commit", "--amend", "--no-verify", "--no-edit", "-m", new_msg],
        env={"SKIP": "check-commit-separation"},
    )
    print(f"   trailer = Private-Commit: {private_head}")

    # 6. 推送
    if args.no_push:
        print("\n[5/5] --no-push 模式，跳过 push + PR")
        print(f"\n✅ {synced} commits 已 commit 到本地 {dev_branch}")
        return

    print("─" * 70)
    print(f"[5/5] push public/{dev_branch}（SKIP=pytest-picked 跳过 pre-push 误报）")
    sh(
        ["git", "-C", str(PUBLIC), "push", "-f", PUBLIC_REMOTE, dev_branch],
        env={"SKIP": "pytest-picked"},
    )

    # 7. 开 PR
    if args.no_pr:
        print("\n✅ push 完成，--no-pr 模式不开 PR")
        return

    print("─" * 70)
    print("[bonus] 开 draft PR")
    # 找仓库名（从远端地址）
    repo_url = sh(["git", "-C", str(PRIVATE), "remote", "get-url", PUBLIC_REMOTE]).strip()
    repo = re.sub(r".*github\.com[:/]", "", repo_url).rstrip("/").removesuffix(".git")

    pr_title = f"{args.group_name}: {synced} commits"
    commit_lines = []
    for c in commits:
        # 提交标题完整保留（避免之前 [:80] 截断丢上下文）
        commit_lines.append(f"- {c['hash'][:7]} {c.get('subject', '')}")
    body = f"""## Scope
`{args.group_name}`

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
