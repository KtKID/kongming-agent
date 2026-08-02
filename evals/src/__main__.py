"""Harness Eval CLI 入口。

# 支持 `python -m evals.src` 和 `python scripts/run_kongming_harness_eval.py` 两种调用。
# 关键函数：build_parser、main。
"""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI parser，输入为空，输出 ArgumentParser。"""

    parser = argparse.ArgumentParser(
        description="Run Kongming harness eval suite via SessionEngine + Runner"
    )
    parser.add_argument(
        "--environment",
        help="Eval environment preset id from evals/harness-runtime-v0.1/environments.yaml",
    )
    parser.add_argument(
        "--environment-config",
        help="Environment preset YAML path; default evals/harness-runtime-v0.1/environments.yaml",
    )
    parser.add_argument("--suite", help="Migration override for suite path")
    parser.add_argument(
        "--mode",
        choices=("fixture",),
        default=None,
        help="无 --preset 时的运行模式；fixture 走内置伪 LLM 验证 harness 闭环",
    )
    parser.add_argument("--preset", "--llm", dest="preset", help="Kongming model catalog preset id")
    parser.add_argument("--config", help="Kongming config path; default config/setting.yaml")
    parser.add_argument("--output-dir")
    parser.add_argument("--run-id")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--profile", choices=("baseline-min", "full"))
    parser.add_argument("--approval-mode", choices=("auto_allow", "interactive", "case"))
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="每题重复跑 N 次以计算 pass^k 可靠性指标；未指定时取 environment 配置值，都无则 fallback 1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，输入 argv，输出进程退出码。"""

    from .runner import run_suite_async

    args = build_parser().parse_args(argv)
    summary = asyncio.run(run_suite_async(args))
    print(f"run_dir: {summary['run_dir']}")
    print(f"passed: {summary['passed']} / {summary['total']}")
    print(f"score: {summary['score']:.2f}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
