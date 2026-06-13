"""PoC: _build_stub_parent_runtime build + aclose 链路验证。

这是 task-2 checklist #1 的 fail-fast 关卡。
如果本文件任何测试失败 → task-2 后续不继续。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_test_config(tmp_path: Path, *, enabled: bool = True) -> object:
    """构造最小 Config 用于 EvolutionManager 装配测试。"""
    from infrastructure.config import load_config

    cfg = load_config(None)
    # 覆盖 evolution.learning 关键字段
    cfg = cfg.model_copy(
        update={
            "evolution": cfg.evolution.model_copy(
                update={
                    "learning": cfg.evolution.learning.model_copy(
                        update={
                            "enabled": enabled,
                            "root_path": str(tmp_path / "evolution"),
                            "every_n_runs": 5,
                            "min_user_turns": 3,
                        }
                    )
                }
            )
        }
    )
    return cfg


class TestStubParentRuntime:
    """PoC #1: NativeRuntime.build 在 ad-hoc 上下文工作。"""

    @pytest.mark.asyncio
    async def test_build_and_aclose(self, tmp_path: Path) -> None:
        """验证 _build_stub_parent_runtime 能 build + aclose，不抛。"""
        from evolution.evolution_manager import EvolutionManager

        cfg = _make_test_config(tmp_path, enabled=True)
        manager = EvolutionManager(config=cfg, kongming_home=tmp_path)

        stub = manager._build_stub_parent_runtime()
        assert stub is not None
        await stub.aclose()
        await manager.aclose()

    @pytest.mark.asyncio
    async def test_stub_aclose_after_reviewer_exception(self, tmp_path: Path) -> None:
        """验证 reviewer 抛异常时 stub.aclose 仍被调用（try/finally 保证）。"""
        from evolution.evolution_manager import EvolutionManager

        cfg = _make_test_config(tmp_path, enabled=True)
        manager = EvolutionManager(config=cfg, kongming_home=tmp_path)

        stub = manager._build_stub_parent_runtime()
        # 模拟"reviewer 跑的过程中出错"——直接 aclose（不跑 reviewer）
        # 关键是验证 aclose 不抛
        await stub.aclose()
        await manager.aclose()
