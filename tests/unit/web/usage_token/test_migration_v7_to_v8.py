"""测试 ``_migration.upgrade_v7_to_v8`` —— v7 → v8 lazy upgrade 纯函数。

覆盖：
- 3 channel fixture：claude_code / codex / generic_chat（cache_creation 启发式两分支）
- 幂等性：v8 不再升级
- 旧字段彻底删除（含 cumulative_total_tokens 派生量丢弃）
- 字段映射规则正确
"""

from __future__ import annotations

from web.usage_token._migration import upgrade_v7_to_v8

# =============================================================================
# claude_code 通道
# =============================================================================


class TestClaudeCodeMigration:
    def test_maps_to_anthropic_channel(self) -> None:
        v7 = {
            "schema_version": 7,
            "backend_kind": "claude_code",
            "cumulative_prompt_tokens": 120000,
            "cumulative_completion_tokens": 3000,
            "cumulative_total_tokens": 123000,
            "cumulative_cache_read_tokens": 88000,
            "cumulative_cache_creation_tokens": 4000,
        }
        v8 = upgrade_v7_to_v8(v7)

        assert v8["schema_version"] == 8
        assert v8["cumulative_usage"] == {
            "channel": "anthropic",
            "input_tokens": 120000,
            "cache_read_input_tokens": 88000,
            "cache_creation_input_tokens": 4000,
            "output_tokens": 3000,
        }
        # 旧字段彻底删除
        assert "cumulative_prompt_tokens" not in v8
        assert "cumulative_completion_tokens" not in v8
        assert "cumulative_total_tokens" not in v8
        assert "cumulative_cache_read_tokens" not in v8
        assert "cumulative_cache_creation_tokens" not in v8

    def test_other_thread_fields_preserved(self) -> None:
        v7 = {
            "schema_version": 7,
            "backend_kind": "claude_code",
            "id": "thread-abc123def456",
            "name": "test",
            "claude_thread_id": "session-xyz",
            "cumulative_prompt_tokens": 100,
            "cumulative_completion_tokens": 20,
        }
        v8 = upgrade_v7_to_v8(v7)
        # thread 其他字段原样保留
        assert v8["id"] == "thread-abc123def456"
        assert v8["name"] == "test"
        assert v8["claude_thread_id"] == "session-xyz"


# =============================================================================
# codex 通道
# =============================================================================


class TestCodexMigration:
    def test_maps_to_openai_channel_with_lossy_alias(self) -> None:
        """codex 旧 cumulative_cache_read 实际是 cached_input 的 lossy 别名。"""
        v7 = {
            "schema_version": 7,
            "backend_kind": "codex",
            "cumulative_prompt_tokens": 120000,
            "cumulative_completion_tokens": 3000,
            "cumulative_total_tokens": 123000,
            "cumulative_cache_read_tokens": 88000,  # → cached_input_tokens
            "cumulative_cache_creation_tokens": 0,
        }
        v8 = upgrade_v7_to_v8(v7)
        assert v8["cumulative_usage"] == {
            "channel": "openai",
            "input_tokens": 120000,
            "cached_input_tokens": 88000,
            "output_tokens": 3000,
            "reasoning_output_tokens": 0,  # 决策 4N：历史不补
        }

    def test_drops_cache_creation_for_codex(self) -> None:
        """Codex 没有 cache_creation 概念，即使有值也丢弃。"""
        v7 = {
            "schema_version": 7,
            "backend_kind": "codex",
            "cumulative_prompt_tokens": 100,
            "cumulative_completion_tokens": 20,
            "cumulative_cache_read_tokens": 0,
            # 历史脏数据：codex 不应有 cache_creation，但容错处理
            "cumulative_cache_creation_tokens": 999,
        }
        v8 = upgrade_v7_to_v8(v7)
        # v8 cumulative_usage 里无 cache_creation_* 字段
        assert "cache_creation_input_tokens" not in v8["cumulative_usage"]


# =============================================================================
# generic_chat 通道 — 启发式判断
# =============================================================================


class TestGenericChatHeuristic:
    def test_cache_creation_nonzero_means_anthropic(self) -> None:
        """cache_creation > 0 → 当作 Anthropic 系（独有概念）。"""
        v7 = {
            "schema_version": 7,
            "backend_kind": "generic_chat",
            "cumulative_prompt_tokens": 100,
            "cumulative_completion_tokens": 20,
            "cumulative_cache_read_tokens": 80,
            "cumulative_cache_creation_tokens": 5,  # > 0 触发 anthropic
        }
        v8 = upgrade_v7_to_v8(v7)
        assert v8["cumulative_usage"]["channel"] == "anthropic"
        assert v8["cumulative_usage"]["cache_read_input_tokens"] == 80
        assert v8["cumulative_usage"]["cache_creation_input_tokens"] == 5

    def test_cache_creation_zero_means_openai(self) -> None:
        """cache_creation == 0 → 当作 OpenAI 系。"""
        v7 = {
            "schema_version": 7,
            "backend_kind": "generic_chat",
            "cumulative_prompt_tokens": 100,
            "cumulative_completion_tokens": 20,
            "cumulative_cache_read_tokens": 30,
            "cumulative_cache_creation_tokens": 0,
        }
        v8 = upgrade_v7_to_v8(v7)
        assert v8["cumulative_usage"]["channel"] == "openai"
        assert v8["cumulative_usage"]["cached_input_tokens"] == 30
        assert v8["cumulative_usage"]["reasoning_output_tokens"] == 0


# =============================================================================
# 幂等 + 边界
# =============================================================================


class TestIdempotent:
    def test_v8_input_returns_unchanged(self) -> None:
        v8 = {
            "schema_version": 8,
            "backend_kind": "claude_code",
            "cumulative_usage": {"channel": "anthropic", "input_tokens": 100},
        }
        result = upgrade_v7_to_v8(v8)
        # 非 v7 不处理
        assert result == v8

    def test_no_schema_version_returns_unchanged(self) -> None:
        data = {"backend_kind": "claude_code"}
        result = upgrade_v7_to_v8(data)
        assert result == data

    def test_does_not_mutate_input(self) -> None:
        original = {
            "schema_version": 7,
            "backend_kind": "claude_code",
            "cumulative_prompt_tokens": 100,
        }
        snapshot = dict(original)
        _ = upgrade_v7_to_v8(original)
        # 输入未被修改
        assert original == snapshot


class TestEdgeCases:
    def test_missing_backend_kind_defaults_generic_chat(self) -> None:
        v7 = {
            "schema_version": 7,
            "cumulative_prompt_tokens": 100,
            "cumulative_cache_creation_tokens": 0,
        }
        v8 = upgrade_v7_to_v8(v7)
        # 缺 backend_kind → 视为 generic_chat → cache_creation=0 → openai
        assert v8["cumulative_usage"]["channel"] == "openai"

    def test_missing_token_fields_default_zero(self) -> None:
        v7 = {"schema_version": 7, "backend_kind": "claude_code"}
        v8 = upgrade_v7_to_v8(v7)
        assert v8["cumulative_usage"]["input_tokens"] == 0
        assert v8["cumulative_usage"]["output_tokens"] == 0

    def test_none_token_fields_default_zero(self) -> None:
        """JSON null 值容错。"""
        v7 = {
            "schema_version": 7,
            "backend_kind": "claude_code",
            "cumulative_prompt_tokens": None,
            "cumulative_completion_tokens": None,
        }
        v8 = upgrade_v7_to_v8(v7)
        assert v8["cumulative_usage"]["input_tokens"] == 0
        assert v8["cumulative_usage"]["output_tokens"] == 0
