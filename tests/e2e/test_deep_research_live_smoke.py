"""Deep Research live smoke scaffold。

本脚本预留真实来源 provider 的低预算 smoke 测试入口。
作用是在配置真实 provider 后验证 search/fetch 基础链路；未配置时给出明确 skip reason。
关键执行流程：读取 KONGMING_DEEP_RESEARCH_LIVE_PROVIDER，缺失时 skip，存在时提示当前 task 只完成 provider 注入点。
关键函数：test_deep_research_live_smoke_provider_configured 检查 live smoke 前置配置。
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.live]


def test_deep_research_live_smoke_provider_configured() -> None:
    """验证 live smoke 前置条件，输入为环境变量，输出为通过或明确 skip。"""
    provider = os.environ.get("KONGMING_DEEP_RESEARCH_LIVE_PROVIDER", "").strip()
    if not provider:
        pytest.skip(
            "KONGMING_DEEP_RESEARCH_LIVE_PROVIDER is not configured; "
            "deep_research live smoke requires a real search/fetch provider adapter."
        )
    pytest.skip(
        f"live provider {provider!r} is configured, but this task only ships the provider "
        "contract and fake provider; real provider adapter verification belongs to the "
        "adapter implementation task."
    )
