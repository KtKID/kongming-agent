"""手测 ospn 4 档审批 prompt — 不接 LLM，不接 SafetyDecisionEngine，直接调底层。

跑法（仓库根目录下）：

    uv run python tests/manual/ospn_prompt_demo.py

会依次模拟两个场景，每次让你输入 [o]/[s]/[p]/[n]：

1. standard 写 untracked 文件（用 ospn 三按钮 UX）
2. elevated 写 .env（用 typed-confirm UX）

按 Ctrl-C 退出。

注意：选 [p] 时会问"Confirm? [y/N]"二次确认；如果你回 N，会降级到 session
（不真写 yaml，避免污染你的 .kongming/config.yaml）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让脚本能 import src/ 下的模块（无需 pip install）
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cli.approval import build_cli_action_prompt  # noqa: E402
from core.contracts import ApprovalRequest  # noqa: E402


def _make_request(*, scenario: str, metadata: dict[str, object]) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="manual-run",
        session_id="manual-sess",
        turn=1,
        call_id=f"manual-{scenario}",
        tool_name="write_file",
        arguments={
            "path": "/tmp/ospn_demo_target.txt",
            "content": "hello world",
        },
        metadata=metadata,
    )


async def _scenario_standard() -> None:
    print("\n" + "=" * 60)
    print("场景 1 / 2: standard ask（普通 untracked 文件写入）")
    print("=" * 60)
    prompt = build_cli_action_prompt()
    request = _make_request(
        scenario="standard",
        metadata={
            "reason": "untracked path requires explicit consent",
            "matched_rule": "boundary-untracked-write",
            "decision_class": "explicit_consent",
            "decision_source": "standard",
            "policy_hint": "standard",
            "suggested_alternatives": [
                "改写到项目内 tracked 文件",
                "改写到 .kongming/work/ 子目录",
            ],
        },
    )
    action = await prompt(request)
    print(f"\n→ 你选了: {action.value}")


async def _scenario_elevated() -> None:
    print("\n" + "=" * 60)
    print("场景 2 / 2: elevated ask（写 .env 类敏感文件）")
    print("=" * 60)
    prompt = build_cli_action_prompt()
    request = _make_request(
        scenario="elevated",
        metadata={
            "reason": "writing .env may leak secrets",
            "matched_rule": "project-env",
            "decision_class": "explicit_consent",
            "decision_source": "elevated",
            "policy_hint": "elevated",
            # confirm_token 真实运行时由 ConsentResolver 用
            # sha256(call_id + matched_rule)[:8] 派生，是 8 位十六进制。
            # 这里手写一个跟真实长度一致的示例。
            "confirm_token": "a3f7c2e0",
        },
    )
    action = await prompt(request)
    print(f"\n→ 你选了: {action.value}")


async def main() -> None:
    print("\n[ospn-demo] 即将连续演示 2 个 prompt 场景")
    print("[ospn-demo] 按 Ctrl-C 可随时退出\n")
    try:
        await _scenario_standard()
        await _scenario_elevated()
    except (KeyboardInterrupt, EOFError):
        print("\n[ospn-demo] 退出")
        return
    print("\n[ospn-demo] 演示完成")


if __name__ == "__main__":
    asyncio.run(main())
