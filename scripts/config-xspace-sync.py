#!/usr/bin/env python3
# scripts/config-xspace-sync.py — XSpace 配置 profile 同步维护入口。
#
# 功能：
#   以 config/setting.yaml 为主配置，检查或维护 config/xspace/setting.yaml 与
#   config/xspace/sync-policy.yaml 的同步决策。
#
# 关键执行流程：
#   1. 解析 review / decision / sync 子命令和配置路径参数。
#   2. 构造 ConfigProfileManager，所有业务逻辑委托给配置模块 Manager。
#   3. review 打印 profile 合同问题；decision 写入显式决策；sync 复制主配置
#      字段到 XSpace profile 并记录 sync-copy。
#
# 关键函数：
#   _build_parser：构造命令行参数定义。
#   _manager_from_args：从参数创建 ConfigProfileManager。
#   _cmd_review：执行 profile 合同检查并打印结果。
#   _cmd_decision：写入 xspace-keep / main-only / sync-copy 决策。
#   _cmd_sync：执行主配置到 XSpace profile 的字段复制。
#   main：脚本入口，分派子命令并返回进程退出码。

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from infrastructure.config.profile_manager import (  # noqa: E402
    ConfigProfileManager,
    ProfileDecisionAction,
    format_review_issues,
)


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器，返回 argparse parser。"""
    parser = argparse.ArgumentParser(
        description="维护 config/setting.yaml 到 config/xspace/setting.yaml 的同步决策。",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=REPO_ROOT / "config" / "setting.yaml",
        help="主配置路径，默认 config/setting.yaml。",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO_ROOT / "config" / "xspace" / "setting.yaml",
        help="XSpace profile 路径，默认 config/xspace/setting.yaml。",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=REPO_ROOT / "config" / "xspace" / "sync-policy.yaml",
        help="同步决策 policy 路径，默认 config/xspace/sync-policy.yaml。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("review", help="检查 profile 与 policy 是否一致。")

    decision_parser = subparsers.add_parser("decision", help="写入或更新一条 policy 决策。")
    decision_parser.add_argument("--path", required=True, help="Config leaf dot-path。")
    decision_parser.add_argument(
        "--action",
        required=True,
        choices=("sync-copy", "xspace-keep", "main-only"),
        help="同步决策动作。",
    )
    decision_parser.add_argument("--reason", required=True, help="决策原因。")

    sync_parser = subparsers.add_parser("sync", help="复制主配置字段到 XSpace profile。")
    sync_parser.add_argument("--path", required=True, help="Config leaf dot-path。")
    sync_parser.add_argument(
        "--reason",
        default="XSpace profile 继承主配置值",
        help="写入 sync-copy 决策的原因。",
    )

    return parser


def _manager_from_args(args: argparse.Namespace) -> ConfigProfileManager:
    """从命令行参数创建 ConfigProfileManager，返回可执行的 Manager。"""
    return ConfigProfileManager(
        source_path=args.source.resolve(),
        target_path=args.target.resolve(),
        policy_path=args.policy.resolve(),
    )


def _cmd_review(args: argparse.Namespace) -> int:
    """执行 profile 合同检查，返回进程退出码。"""
    manager = _manager_from_args(args)
    review = manager.review()
    print(
        f"source_leaf_count={review.source_leaf_count} "
        f"target_leaf_count={review.target_leaf_count} "
        f"decision_count={review.decision_count}"
    )
    if review.ok:
        print("review=pass")
        return 0
    print("review=fail")
    print(format_review_issues(review.issues))
    return 1


def _cmd_decision(args: argparse.Namespace) -> int:
    """写入一条 policy 决策，返回进程退出码。"""
    manager = _manager_from_args(args)
    manager.write_decision(
        path=args.path,
        action=cast(ProfileDecisionAction, args.action),
        reason=args.reason,
    )
    print(f"decision=written path={args.path} action={args.action}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    """执行字段复制并记录 sync-copy 决策，返回进程退出码。"""
    manager = _manager_from_args(args)
    manager.sync_copy(path=args.path, reason=args.reason)
    print(f"sync=written path={args.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """脚本主入口，解析参数并分派子命令。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            return _cmd_review(args)
        if args.command == "decision":
            return _cmd_decision(args)
        if args.command == "sync":
            return _cmd_sync(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
