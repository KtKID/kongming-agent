"""e2e：EvolutionManager + ClaudeTranscriptProvider 真实 LLM 链路。

环境要求：
- KONGMING_E2E_REAL_MODEL=1（否则 skip）
- .env 里配好 EVOLUTION_REVIEWER_* 或主模型可用

验证：
1. EvolutionManager 按 cadence 触发 reviewer
2. ClaudeTranscriptProvider 读 fixture jsonl
3. reviewer 子 agent 用真实 LLM 调 evolution_write
4. .kongming/evolution/reviews/ 出现 review 文件
5. 全程日志写到 .kongming/logs/evolution.log
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from evolution.claude_transcript_provider import ClaudeTranscriptProvider
from evolution.evolution_manager import EvolutionManager
from infrastructure.config import load_config

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.getenv("KONGMING_E2E_REAL_MODEL") != "1",
        reason="需要 KONGMING_E2E_REAL_MODEL=1 + 真实 LLM 服务",
    ),
]

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "claude_jsonl"
CLAUDE_THREAD_ID = "test-claude-session-001"
CWD = "test-cwd"


def _setup_claude_home(tmp_path: Path) -> Path:
    """在 tmp_path 下搭 claude home 目录结构 + 复制 fixture jsonl。"""
    from hosts.web.integrations.claude_code.jsonl_history import encode_cwd

    claude_home = tmp_path / "claude_home"
    encoded = encode_cwd(CWD)
    target_dir = claude_home / "projects" / encoded
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        FIXTURE_DIR / "clean_5_turns.jsonl",
        target_dir / f"{CLAUDE_THREAD_ID}.jsonl",
    )
    return claude_home


def _make_config(tmp_path: Path) -> object:
    """加载真实配置 + 覆盖 evolution 参数（低频触发，快速超时）。"""
    cfg = load_config(None)
    return cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": True,
                            "every_n_runs": 2,
                            "min_user_turns": 1,
                            "max_history_messages": 10,
                            "max_nutrients": 2,
                            "review_timeout_seconds": 60.0,
                            "drain_on_close_seconds": 65.0,
                            "root_path": str(tmp_path / "kongming" / "evolution"),
                        }
                    )
                }
            )
        }
    )


@pytest.mark.asyncio
async def test_evolution_claude_channel_real_llm(tmp_path: Path) -> None:
    """端到端：真实 LLM reviewer 在 claude 频道触发。

    步骤：
    1. 搭 fixture jsonl + 临时 kongming home
    2. 构造 EvolutionManager（every_n_runs=2）
    3. 发 2 次 notify_user_message（第 2 次触发 reviewer）
    4. 等 reviewer 完成
    5. 断言 reviews/ 目录出现文件
    6. 断言 evolution.log 存在且有内容
    """
    claude_home = _setup_claude_home(tmp_path)
    kongming_home = tmp_path / "kongming"
    kongming_home.mkdir(parents=True, exist_ok=True)
    cfg = _make_config(tmp_path)

    manager = EvolutionManager(config=cfg, kongming_home=kongming_home)
    assert manager.enabled

    thread_id = "thread-e2etest00001"
    provider = ClaudeTranscriptProvider(
        thread_id=thread_id,
        claude_thread_id=CLAUDE_THREAD_ID,
        cwd=CWD,
        claude_home=claude_home,
    )

    # 发 2 次消息触发 cadence（every_n_runs=2, min_user_turns=1）
    for _ in range(2):
        await manager.notify_user_message(
            thread_id=thread_id,
            provider=provider,
            cwd=CWD,
        )

    # 等 reviewer 后台完成（真实 LLM 可能需要 30-60 秒）
    await asyncio.sleep(2.0)  # 先等一下让 task 启动
    await manager.aclose()  # drain 会等后台 task 完成

    # 断言 1：reviews/ 出现 review 文件
    reviews_dir = kongming_home / "evolution" / "reviews"
    review_files = list(reviews_dir.glob("run-claude-*.json")) if reviews_dir.exists() else []
    assert len(review_files) >= 1, (
        f"期望至少 1 个 review 文件，实际 {len(review_files)}。"
        f"reviews_dir={reviews_dir}, exists={reviews_dir.exists()}"
    )

    # 断言 2：review 文件内容合法
    review_data = json.loads(review_files[0].read_text())
    assert "result" in review_data or "status" in review_data

    # 断言 3：evolution.log 存在且有内容
    log_file = kongming_home / "logs" / "evolution.log"
    assert log_file.exists(), f"evolution.log 不存在：{log_file}"
    log_content = log_file.read_text()
    assert "evolution cadence" in log_content, "日志里应该有 cadence 记录"
    assert "evolution triggered" in log_content, "日志里应该有触发记录"
    assert thread_id in log_content, "日志里应该有 thread_id"

    # 打印日志摘要（pytest -s 可见）
    print(f"\n{'=' * 60}")
    print(f"Evolution e2e 完成：{len(review_files)} 个 review 文件")
    print(f"日志文件：{log_file} ({log_file.stat().st_size} bytes)")
    print("日志前 20 行：")
    for line in log_content.splitlines()[:20]:
        print(f"  {line}")
    print(f"{'=' * 60}")


@pytest.mark.asyncio
async def test_evolution_log_file_captures_all_stages(tmp_path: Path) -> None:
    """验证 evolution.log 全量捕获 cadence → trigger → reviewer 各阶段日志。"""
    claude_home = _setup_claude_home(tmp_path)
    kongming_home = tmp_path / "kongming"
    kongming_home.mkdir(parents=True, exist_ok=True)
    cfg = _make_config(tmp_path)

    manager = EvolutionManager(config=cfg, kongming_home=kongming_home)

    thread_id = "thread-logtest00001"
    provider = ClaudeTranscriptProvider(
        thread_id=thread_id,
        claude_thread_id=CLAUDE_THREAD_ID,
        cwd=CWD,
        claude_home=claude_home,
    )

    # 第 1 次：不触发（cadence miss）
    await manager.notify_user_message(thread_id=thread_id, provider=provider, cwd=CWD)

    # 第 2 次：触发 reviewer
    await manager.notify_user_message(thread_id=thread_id, provider=provider, cwd=CWD)

    await asyncio.sleep(2.0)
    await manager.aclose()

    log_file = kongming_home / "logs" / "evolution.log"
    assert log_file.exists()
    log_content = log_file.read_text()

    # 验证全阶段日志
    assert "evolution cadence: thread=" in log_content, "缺 cadence 计数日志"
    assert "run_count=1" in log_content, "缺第 1 次计数"
    assert "run_count=2" in log_content, "缺第 2 次计数"
    assert "trigger blocked:" in log_content or "evolution triggered:" in log_content, (
        "缺跳过/触发日志"
    )

    print(f"\n日志全文 ({log_file}):")
    print(log_content)
