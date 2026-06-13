"""unit：``ModelConfig.effective_provider`` URL 启发式判定。

覆盖 8+ 种典型 URL 形态：

- 用户声明 ``openai_compatible`` 但 URL 命中 anthropic 启发式 → 自动切 ``anthropic``
- 用户声明 ``openai_compatible``，URL 不命中启发式 → 保留声明值
- 用户声明 ``anthropic`` 不论 URL 如何 → 保留声明值
- 同一 host（``api.minimaxi.com``）不同路径 → 启发式按路径区分

启发式两条规则（互斥触发 anthropic）：

1. ``host == api.anthropic.com``（Anthropic 官方域名硬编码）
2. ``urlparse(base_url).path`` 切段后任一独立段为 ``"anthropic"``
"""

from __future__ import annotations

import pytest

from infrastructure.config.models import ModelConfig

# ---------------------------------------------------------------------------
# host 启发式：api.anthropic.com 自动切到 anthropic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anthropic_official_host_overrides_to_anthropic() -> None:
    """官方域名 ``api.anthropic.com`` 不论用户声明都走 anthropic。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_anthropic_official_host_with_path_still_anthropic() -> None:
    """``api.anthropic.com/v1`` 这类带路径的官方域名也命中 host 规则。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="claude-sonnet-4-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-xxx",
    )
    assert cfg.effective_provider == "anthropic"


# ---------------------------------------------------------------------------
# path 启发式：路径段含 "anthropic" 自动切到 anthropic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_third_party_anthropic_path_overrides_to_anthropic() -> None:
    """第三方 base_url 路径末段 ``/anthropic`` 触发自动切。

    例如 MiniMax 的 anthropic 兼容端点。
    """
    cfg = ModelConfig(
        provider="openai_compatible",
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/anthropic",
        api_key="sk-cp-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_third_party_anthropic_path_with_version_still_anthropic() -> None:
    """``/anthropic/v1`` 这种版本号后缀不影响判定（path 切段后段仍命中）。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/anthropic/v1",
        api_key="sk-cp-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_third_party_anthropic_path_in_middle_segment_still_anthropic() -> None:
    """``/anthropic`` 出现在路径中间段也算（启发式按段独立判断，不要求位置）。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="some-model",
        base_url="https://example.com/api/anthropic/v2",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "anthropic"


# ---------------------------------------------------------------------------
# 反例：不命中启发式时保留用户声明值
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_minimax_openai_path_keeps_openai_declared() -> None:
    """同 host 不同路径：MiniMax OpenAI 端点（无 ``anthropic`` 段）保留用户声明。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/v1",
        api_key="sk-cp-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_local_lm_studio_keeps_openai_declared() -> None:
    """本地 LM Studio：``http://127.0.0.1:1234/v1`` 不含 anthropic → 保留用户声明。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="gemma-4-e4b-it",
        base_url="http://127.0.0.1:1234/v1",
        api_key="",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_glm_endpoint_keeps_openai_declared() -> None:
    """智谱 ``open.bigmodel.cn/api/paas/v4`` 不含 anthropic → 保留用户声明。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="glm-5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_openai_official_keeps_openai_declared() -> None:
    """OpenAI 官方 ``api.openai.com/v1`` 不应被启发式触发。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


# ---------------------------------------------------------------------------
# 用户声明 anthropic：不论 URL 如何都保留用户声明（启发式只升级，不降级）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_declared_anthropic_kept_even_for_arbitrary_url() -> None:
    """用户声明 anthropic + URL 既不含 anthropic 段、不以 vN 结尾、host 不在路由表 →
    走 declare 兜底（4 级优先级第 3 级）。

    URL 选择说明：
      - host 是 ``custom-proxy.example.com``，**不在内置路由表**
      - path 段是 ``["messages"]``，不含 ``anthropic``、不命中 ``^v\\d+$``
      - 所以 path 启发式 + host 路由表都返回 None → 走 declare → anthropic
    """
    cfg = ModelConfig(
        provider="anthropic",
        name="claude-sonnet-4-5",
        base_url="https://custom-proxy.example.com/messages",
        api_key="sk-ant-xxx",
    )
    assert cfg.effective_provider == "anthropic"


# ---------------------------------------------------------------------------
# 边界：子串误命中防御（"anthropic" 必须是独立段，不接受子串）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_substring_anthropic_in_segment_does_not_trigger() -> None:
    """``/my-anthropic-proxy/v1`` 的 ``my-anthropic-proxy`` 是子串包含 "anthropic"，
    但它本身不是独立段为 ``"anthropic"`` —— 不应触发自动切。

    防御点：避免业务命名误命中启发式（例如某公司起了带 anthropic 的代理名）。
    """
    cfg = ModelConfig(
        provider="openai_compatible",
        name="custom",
        base_url="https://example.com/my-anthropic-proxy/v1",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_case_insensitive_path_segment_match() -> None:
    """路径段 ``ANTHROPIC`` 大写也应匹配（启发式不区分大小写，user-friendly）。"""
    cfg = ModelConfig(
        provider="openai_compatible",
        name="some-model",
        base_url="https://example.com/ANTHROPIC/v1",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "anthropic"


# ===========================================================================
# v2 新增：双向 path 启发式 + host 路由表 + 4 级优先级
# ===========================================================================


# ---------------------------------------------------------------------------
# v2 path 降级启发式：URL 末尾段是 ^v\d+$ → openai_compatible
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("version_segment", ["v1", "v2", "v3", "v4", "v10", "v999"])
def test_path_ending_with_version_segment_downgrades_to_openai(version_segment: str) -> None:
    """URL 末尾段命中 ``^v\\d+$`` → openai_compatible（覆盖 declare）。

    项目内既有约定：anthropic base_url **不带版本段**，openai 协议 base_url
    **带版本段**。所以"末尾 vN"是 OpenAI 协议的强信号，应当降级。
    """
    cfg = ModelConfig(
        provider="anthropic",  # 用户故意写错
        name="some-model",
        base_url=f"https://example.com/api/{version_segment}",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_glm_endpoint_with_wrong_declare_is_rescued() -> None:
    """**关键回归 case**：用户 declare anthropic + GLM endpoint /v4 →
    path 降级启发式覆盖到 openai_compatible（修复 v1 task 漏掉的故障）。
    """
    cfg = ModelConfig(
        provider="anthropic",  # 用户切端点忘改 yaml
        name="glm-5",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_path_middle_version_segment_does_not_downgrade() -> None:
    """中间段是 vN（非末尾段）不触发降级。

    例如 ``/api/v1/something`` 末尾段是 ``something``，不命中 ``^v\\d+$`` —— 这
    是 openai_compatible 还是 anthropic 不该由这个 path 决定。
    """
    cfg = ModelConfig(
        provider="anthropic",
        name="some-model",
        base_url="https://example.com/v1/something",
        api_key="sk-xxx",
    )
    # path 启发式无结论 → host 不在路由表 → declare 兜底 → anthropic
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_anthropic_path_takes_priority_over_version_segment() -> None:
    """同时含 anthropic 段 + 末尾 vN（如 /anthropic/v1）→ anthropic 升级优先。

    覆盖 MiniMax Anthropic 端点 ``https://api.minimaxi.com/anthropic/v1`` 这种
    误写场景：尽管末尾是 v1，含 anthropic 段是更强信号。
    """
    cfg = ModelConfig(
        provider=None,
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/anthropic/v1",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "anthropic"


# ---------------------------------------------------------------------------
# v2 host 路由表：path 无强信号时按 host 查表
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_host_routing_table_openai_official_bare_host() -> None:
    """``https://api.openai.com``（无路径）→ host 表命中 → openai_compatible。

    path 启发式无信号（segments 为空），host 路由表命中。
    """
    cfg = ModelConfig(
        provider=None,
        name="gpt-4o",
        base_url="https://api.openai.com",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_host_routing_table_glm_bare_host() -> None:
    """``https://open.bigmodel.cn``（无路径）→ host 表命中 → openai_compatible。"""
    cfg = ModelConfig(
        provider=None,
        name="glm-5",
        base_url="https://open.bigmodel.cn",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_host_routing_table_localhost_bare() -> None:
    """``http://localhost:1234``（无路径）→ host 表命中 → openai_compatible。"""
    cfg = ModelConfig(
        provider=None,
        name="gemma",
        base_url="http://localhost:1234",
        api_key="",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_host_routing_table_unknown_host_falls_through() -> None:
    """未知 host + path 无强信号 + provider=None → 默认 openai_compatible。"""
    cfg = ModelConfig(
        provider=None,
        name="unknown-model",
        base_url="https://unknown-vendor.example.com/messages",
        api_key="sk-xxx",
    )
    # path: ["messages"] 无信号；host 不在表；provider=None → 默认
    assert cfg.effective_provider == "openai_compatible"


# ---------------------------------------------------------------------------
# v2 优先级：path > host 表（同 host 双协议靠 path 区分）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_anthropic_priority_over_host_table_minimax() -> None:
    """MiniMax host 表里默认 openai；但 ``/anthropic`` 路径走 path 升级，
    覆盖 host 表 → anthropic。
    """
    cfg = ModelConfig(
        provider=None,
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/anthropic",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_path_version_segment_priority_over_host_table_minimax() -> None:
    """MiniMax host 表里默认 openai；``/v1`` 路径走 path 降级，结果一致 → openai_compatible。"""
    cfg = ModelConfig(
        provider=None,
        name="MiniMax-M2.7",
        base_url="https://api.minimaxi.com/v1",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


# ---------------------------------------------------------------------------
# v2 provider 字段 Optional：默认 None 时走启发式 / host 表 / 默认
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_default_none_with_anthropic_url() -> None:
    """provider 字段不传 → URL 启发式决定。"""
    cfg = ModelConfig(
        name="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
    )
    assert cfg.provider is None
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_provider_default_none_with_local_url() -> None:
    """provider=None + 本地 URL → host 表命中 → openai_compatible。"""
    cfg = ModelConfig(
        name="gemma",
        base_url="http://127.0.0.1:1234/v1",
        api_key="",
    )
    assert cfg.provider is None
    assert cfg.effective_provider == "openai_compatible"


# ---------------------------------------------------------------------------
# v2 用户 yaml 扩展 provider_routing：与默认表合并去重
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_provider_routing_merges_with_default() -> None:
    """用户在 yaml 写新 host → 默认 + 新增同时生效。"""
    cfg = ModelConfig(
        name="my-model",
        base_url="https://my-llm-vendor.example.com",
        api_key="sk-xxx",
        provider_routing={"openai_compatible": ["my-llm-vendor.example.com"]},
    )
    # 用户新加的 host 命中
    assert cfg.effective_provider == "openai_compatible"
    # 默认表的 host 仍然在（合并而不是替换）
    flat = cfg._host_to_protocol
    assert flat["api.openai.com"] == "openai_compatible"
    assert flat["api.anthropic.com"] == "anthropic"
    assert flat["my-llm-vendor.example.com"] == "openai_compatible"


@pytest.mark.unit
def test_user_provider_routing_extends_anthropic_group() -> None:
    """用户可往 anthropic 组追加 host。"""
    cfg = ModelConfig(
        name="my-claude-proxy",
        base_url="https://my-anthropic-proxy.internal",
        api_key="sk-xxx",
        provider_routing={"anthropic": ["my-anthropic-proxy.internal"]},
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_user_provider_routing_dedup() -> None:
    """用户重复添加默认表已有的 host 不会产生重复条目。"""
    cfg = ModelConfig(
        name="gpt-4o",
        base_url="https://api.openai.com",
        api_key="sk-xxx",
        provider_routing={"openai_compatible": ["api.openai.com", "api.openai.com"]},
    )
    # 计数：默认表 7 个 + 用户重复加的去重后 0 = 7
    openai_hosts = cfg.provider_routing["openai_compatible"]
    assert openai_hosts.count("api.openai.com") == 1


# ---------------------------------------------------------------------------
# v2 4 级优先级整合：完整路径全过一遍
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_priority_level_1_path_strong_signal() -> None:
    """L1：path 强信号最高，覆盖 host 表 + declare。"""
    cfg = ModelConfig(
        provider="openai_compatible",  # declare 故意写错
        name="claude",
        base_url="https://api.anthropic.com",  # host = api.anthropic.com → anthropic
        api_key="sk-ant-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_priority_level_2_host_table_when_path_silent() -> None:
    """L2：path 无信号时走 host 表。"""
    cfg = ModelConfig(
        provider="anthropic",  # declare 故意写错
        name="gpt-4o",
        base_url="https://api.openai.com",  # path 无信号；host 表命中 openai
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"


@pytest.mark.unit
def test_priority_level_3_declare_when_path_and_host_silent() -> None:
    """L3：path + host 都无信号 → 用 declare。"""
    cfg = ModelConfig(
        provider="anthropic",
        name="custom",
        base_url="https://unknown-vendor.example.com/messages",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "anthropic"


@pytest.mark.unit
def test_priority_level_4_default_when_everything_silent() -> None:
    """L4：path + host + declare 都无信号 → 默认 openai_compatible。"""
    cfg = ModelConfig(
        provider=None,
        name="custom",
        base_url="https://unknown-vendor.example.com/messages",
        api_key="sk-xxx",
    )
    assert cfg.effective_provider == "openai_compatible"
