"""unit：``executors.llm.raw_dump`` 的开关、落盘、脱敏、静默失败行为。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from executors.llm.raw_dump import dump_raw_llm_interaction, is_enabled


@pytest.mark.unit
def test_is_enabled_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 没设或不是 '1' 时都返回 False。"""
    monkeypatch.delenv("KONGMING_TRACE_RAW_LLM", raising=False)
    assert is_enabled() is False

    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "0")
    assert is_enabled() is False

    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "true")
    # 严格要求 "1"，避免"truthy" 字符串的歧义解读
    assert is_enabled() is False


@pytest.mark.unit
def test_is_enabled_on_when_env_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")
    assert is_enabled() is True


@pytest.mark.unit
def test_dump_disabled_returns_none_and_creates_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env 没开时应直接返回 None，不碰磁盘。"""
    monkeypatch.delenv("KONGMING_TRACE_RAW_LLM", raising=False)

    result = dump_raw_llm_interaction(
        provider="openai_responses",
        url="https://example.com/v1/chat/completions",
        request_payload={"model": "stub", "messages": []},
        request_headers={"Authorization": "Bearer sk-xyz"},
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body={"choices": []},
        dump_dir=tmp_path,
    )
    assert result is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_dump_enabled_writes_full_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """开启后：文件产出、路径可返回、字段齐全、内容可 JSON 回读。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    path = dump_raw_llm_interaction(
        provider="openai_responses",
        url="https://example.com/v1/chat/completions",
        request_payload={"model": "glm-5.1", "messages": [{"role": "user", "content": "hi"}]},
        request_headers={"Authorization": "Bearer sk-xyz", "Content-Type": "application/json"},
        response_status=200,
        response_headers={"content-type": "application/json", "X-LOG-ID": "test-1"},
        response_body={
            "id": "call_-7695903923470590663",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"total_tokens": 12},
        },
        dump_dir=tmp_path,
    )

    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path
    assert path.name.startswith("raw-llm-")
    assert path.suffix == ".json"

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["provider"] == "openai_responses"
    assert record["url"] == "https://example.com/v1/chat/completions"
    assert record["request"]["payload"]["model"] == "glm-5.1"
    assert record["request"]["payload"]["messages"][0]["content"] == "hi"
    assert record["response"]["status_code"] == 200
    assert record["response"]["headers"]["X-LOG-ID"] == "test-1"
    assert record["response"]["body"]["id"] == "call_-7695903923470590663"
    assert record["response"]["body"]["usage"]["total_tokens"] == 12
    assert record["error"] is None


@pytest.mark.unit
def test_dump_redacts_sensitive_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Authorization / X-API-Key / Api-Key 的值必须被脱敏为 <redacted>。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    path = dump_raw_llm_interaction(
        provider="openai_responses",
        url="https://example.com/v1/chat/completions",
        request_payload={},
        request_headers={
            "Authorization": "Bearer sk-real-secret-DO-NOT-LEAK",
            "x-api-key": "zh-real-key-DO-NOT-LEAK",
            "API-KEY": "another-DO-NOT-LEAK",  # 大小写不敏感
            "Content-Type": "application/json",
        },
        response_status=200,
        response_headers=None,
        response_body={},
        dump_dir=tmp_path,
    )

    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8"))
    headers = record["request"]["headers"]

    assert headers["Authorization"] == "<redacted>"
    assert headers["x-api-key"] == "<redacted>"
    assert headers["API-KEY"] == "<redacted>"
    # 非敏感字段保留原值
    assert headers["Content-Type"] == "application/json"

    # 防御性：原始 key 字节序列不应出现在 dump 文件里任何位置
    content = path.read_text(encoding="utf-8")
    assert "sk-real-secret-DO-NOT-LEAK" not in content
    assert "zh-real-key-DO-NOT-LEAK" not in content
    assert "another-DO-NOT-LEAK" not in content


@pytest.mark.unit
def test_dump_records_error_for_non_2xx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """4xx/5xx 场景：error 字段被填充；body 里可以传占位文本。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    path = dump_raw_llm_interaction(
        provider="openai_responses",
        url="https://example.com/v1/chat/completions",
        request_payload={"model": "glm-5.1"},
        request_headers={},
        response_status=401,
        response_headers={"content-type": "application/json"},
        response_body={"error": {"message": "Invalid API key"}},
        error="HTTP 401",
        dump_dir=tmp_path,
    )

    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["response"]["status_code"] == 401
    assert record["response"]["body"]["error"]["message"] == "Invalid API key"
    assert record["error"] == "HTTP 401"


@pytest.mark.unit
def test_dump_never_raises_on_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """dump 内部任何异常（如磁盘不可写）都必须静默返回 None，不影响主链路。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    # 指向一个不可写路径（把 dump_dir 指成已存在的普通文件，mkdir 会失败）
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("I am a file, not a directory")

    result = dump_raw_llm_interaction(
        provider="openai_responses",
        url="https://example.com",
        request_payload={},
        request_headers={},
        response_status=200,
        response_headers={},
        response_body={},
        dump_dir=blocked,  # mkdir 会 FileExistsError
    )

    # 不抛；静默返回 None
    assert result is None


@pytest.mark.unit
def test_dump_enabled_param_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式传 ``enabled=True`` 即使 env 没设也应该 dump（config 驱动路径）。"""
    monkeypatch.delenv("KONGMING_TRACE_RAW_LLM", raising=False)

    path = dump_raw_llm_interaction(
        enabled=True,
        provider="openai_responses",
        url="https://example.com",
        request_payload={},
        request_headers={},
        response_status=200,
        response_headers={},
        response_body={},
        dump_dir=tmp_path,
    )
    assert path is not None
    assert path.exists()


@pytest.mark.unit
def test_dump_enabled_false_param_overrides_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式传 ``enabled=False`` 即使 env=1 也应该不 dump（让调用方精确控制）。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    path = dump_raw_llm_interaction(
        enabled=False,
        provider="openai_responses",
        url="https://example.com",
        request_payload={},
        request_headers={},
        response_status=200,
        response_headers={},
        response_body={},
        dump_dir=tmp_path,
    )
    assert path is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_dump_unique_filenames_across_rapid_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一秒内连续 dump 多次应各自产出不同文件（靠 uuid nonce 保证）。"""
    monkeypatch.setenv("KONGMING_TRACE_RAW_LLM", "1")

    paths = set()
    for _ in range(5):
        p = dump_raw_llm_interaction(
            provider="openai_responses",
            url="https://example.com",
            request_payload={},
            request_headers={},
            response_status=200,
            response_headers={},
            response_body={},
            dump_dir=tmp_path,
        )
        assert p is not None
        paths.add(p)

    assert len(paths) == 5, "5 次 dump 应生成 5 个不同文件（nonce 冲突概率极低）"
