"""验证 ``web.usage_token`` 包的封装边界。

核心断言：``web.usage_token.__init__`` 不暴露内部模型 / helper。
``.importlinter`` 是 CI gate；本测试是 runtime smoke 兜底。
"""

from __future__ import annotations


class TestPackageInitExportSurface:
    def test_only_public_symbols_exported(self) -> None:
        """__init__.py 只 export 6 个公共符号（v0.1 加 DeriveProvider）。"""
        from web import usage_token

        expected = {
            "DeriveProvider",
            "UsageTokenManager",
            "UsageTokenSnapshot",
            "ThreadUsageSummary",
            "UsageFrame",
            "ThreadMetadataIO",
        }
        assert set(usage_token.__all__) == expected

    def test_internal_models_not_in_package_namespace(self) -> None:
        """私有 model 不出现在 package 命名空间下。"""
        from web import usage_token

        # 这些是私有的，外部不应该通过 usage_token.xxx 访问
        assert not hasattr(usage_token, "_AnthropicTokenUsage")
        assert not hasattr(usage_token, "_OpenAITokenUsage")
        assert not hasattr(usage_token, "TokenUsage")
        # helper / migration 也不暴露
        assert not hasattr(usage_token, "to_persisted_dict")
        assert not hasattr(usage_token, "from_persisted_dict")
        assert not hasattr(usage_token, "empty_usage_for_channel")
        assert not hasattr(usage_token, "upgrade_v7_to_v8")

    def test_public_dtos_are_pydantic_models(self) -> None:
        """公共 DTO 是 frozen Pydantic BaseModel（不可被外部当 mutable 用）。"""
        from pydantic import BaseModel

        from web.usage_token import (
            ThreadUsageSummary,
            UsageFrame,
            UsageTokenSnapshot,
        )

        for cls in (UsageTokenSnapshot, ThreadUsageSummary, UsageFrame):
            assert issubclass(cls, BaseModel)
            # frozen=True 阻止 mutate
            assert cls.model_config.get("frozen") is True

    def test_manager_class_signature_does_not_leak_token_usage(self) -> None:
        """UsageTokenManager 公共方法返回的不是 TokenUsage 实例。"""
        import inspect

        from web.usage_token import UsageTokenManager

        sig = inspect.signature(UsageTokenManager.record_run_usage)
        # 返回类型注解应是 UsageTokenSnapshot 字符串，不是 TokenUsage / 私有 model
        assert "UsageTokenSnapshot" in str(sig.return_annotation)


class TestProtocolCompatibility:
    """ThreadMetadataIO 是 runtime_checkable Protocol——外部 adapter 实现要兼容。"""

    def test_protocol_runtime_checkable(self) -> None:
        from web.usage_token import ThreadMetadataIO

        class DummyAdapter:
            async def read_cumulative_usage(self, thread_id: str):  # type: ignore[no-untyped-def]
                return None

            async def write_cumulative_usage(self, thread_id: str, usage_dict):  # type: ignore[no-untyped-def]
                pass

            async def read_last_run_snapshot(self, thread_id: str):  # type: ignore[no-untyped-def]
                return None

            async def write_last_run_snapshot(self, thread_id: str, snapshot_dict):  # type: ignore[no-untyped-def]
                pass

            async def read_last_model_name(self, thread_id: str):  # type: ignore[no-untyped-def]
                return ""

            async def write_last_model_name(self, thread_id: str, model_name):  # type: ignore[no-untyped-def]
                pass

        assert isinstance(DummyAdapter(), ThreadMetadataIO)

    def test_protocol_rejects_incomplete_implementation(self) -> None:
        from web.usage_token import ThreadMetadataIO

        class IncompleteAdapter:
            async def read_cumulative_usage(self, thread_id: str):  # type: ignore[no-untyped-def]
                return None

            # 缺 write_cumulative_usage

        assert not isinstance(IncompleteAdapter(), ThreadMetadataIO)
