"""通用频道 FileSession 到司天报告 API 的端到端闭环测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.contracts import LLMRequest, LLMResponse
from core.message import Message, ToolCall
from hosts.web.routers.sitian import router as sitian_router
from hosts.web.threads.metadata import ThreadMetadata, write_thread_metadata
from infrastructure.config.models import Config
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap
from sitian.config import SiTianAnalyzerConfig, SiTianConfig, SiTianSourceConfig, SiTianSourceKind
from sitian.service import SiTianRunOnce
from sitian.store import SiTianRecordsStore


class _GenericChatAnalyzerProvider:
    """记录输入并返回针对真实 workspace 的确定性司天分析结果。"""

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """保存 Analyzer 请求，返回可被报告模型规范化的 JSON。"""
        self.requests.append(request)
        return LLMResponse(
            message=Message.assistant(
                json.dumps(
                    {
                        "summary": "通用频道项目已分析",
                        "alerts": [],
                        "projects": [
                            {
                                "projectId": self._project_id,
                                "projectName": "generic-workspace",
                                "statusReason": "recent generic chat activity",
                                "narrative": "会话包含待处理的通用频道工作。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        )


def _bootstrap(cwd: Path) -> SessionBootstrap:
    """构造真实 FileSession 落盘所需的不可变初始化元数据。"""
    return SessionBootstrap(
        agent_name="generic-chat-e2e",
        model_name="fake-model",
        instruction_sources=[],
        instruction_text_hash="sha256:e2e",
        created_at=datetime.now(UTC).timestamp(),
        cwd=str(cwd),
    )


def _web_config() -> Config:
    """构造报告路由读取 generic 子目录的真实 Web 配置对象。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "fake-model"},
            "web": {"enabled": True, "dev_mode": True},
            "scheduler": {"enabled": False},
            "sitian": {"output_subdir": "generic"},
        }
    )


@pytest.mark.e2e
async def test_generic_chat_filesession_runs_analysis_and_serves_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata + FileSession → 司天 → 存储 → 真实 GET /api/sitian/report。"""
    kongming_home = tmp_path / ".kongming"
    workspace = (tmp_path / "generic-workspace").resolve()
    workspace.mkdir()
    thread_id = "thread-a1b2c3d4e5f6"
    now = datetime.now(UTC)
    write_thread_metadata(
        kongming_home,
        ThreadMetadata(
            id=thread_id,
            name="通用频道真实会话",
            preset_id="fake-model",
            backend_kind="generic_chat",
            thread_kind="chat",
            created_at=now.timestamp(),
            updated_at=now.timestamp(),
            message_count=6,
        ),
    )
    session = FileSession(thread_id, _bootstrap(workspace), str(kongming_home / "sessions"))
    await session.append(Message.system("SYSTEM_SECRET_SHOULD_NOT_REACH_SITIAN"))
    await session.append(
        Message.user(
            "请分析通用频道里的真实工作项",
            metadata={"attachments": [{"asset_id": "ATTACHMENT_METADATA_SHOULD_NOT_REACH_SITIAN"}]},
        )
    )
    await session.append(
        Message.assistant(
            tool_calls=[ToolCall(call_id="tool-1", tool_name="read_file", arguments={"path": "x"})]
        )
    )
    await session.append(Message.tool_result("tool-1", "TOOL_RESULT_SHOULD_NOT_REACH_SITIAN"))
    await session.append(Message.assistant("我会从会话中归纳待处理事项。"))
    jsonl_path = kongming_home / "sessions" / thread_id / f"{thread_id}.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"message": {"role": "user", "content": "UNKNOWN_RECORD_SHOULD_NOT_REACH_SITIAN"}},
                ensure_ascii=False,
            )
            + "\n"
        )

    provider = _GenericChatAnalyzerProvider(str(workspace))
    records = SiTianRecordsStore(kongming_home / "sitian" / "generic")
    result = await SiTianRunOnce(
        SiTianConfig(
            output_subdir="generic",
            sources=[
                SiTianSourceConfig(
                    id="generic-chat",
                    kind=SiTianSourceKind.GENERIC_CHAT,
                    path=str(kongming_home),
                )
            ],
            analyzer=SiTianAnalyzerConfig(
                enabled=True,
                preset_id="fake-model",
                skip_if_unchanged=False,
            ),
        ),
        store=records,
        now=now,
        llm_provider=provider,
    )

    assert result.failed_sources == {}
    assert result.observation_count == 2
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[-1].content is not None
    assert "请分析通用频道里的真实工作项" in provider.requests[0].messages[-1].content
    assert "SYSTEM_SECRET_SHOULD_NOT_REACH_SITIAN" not in provider.requests[0].messages[-1].content
    assert (
        "ATTACHMENT_METADATA_SHOULD_NOT_REACH_SITIAN"
        not in provider.requests[0].messages[-1].content
    )
    assert "TOOL_RESULT_SHOULD_NOT_REACH_SITIAN" not in provider.requests[0].messages[-1].content
    assert "UNKNOWN_RECORD_SHOULD_NOT_REACH_SITIAN" not in provider.requests[0].messages[-1].content

    monkeypatch.setenv("KONGMING_HOME", str(kongming_home))
    app = FastAPI()
    app.state.config = _web_config()
    app.include_router(sitian_router)
    with TestClient(app) as client:
        response = client.get("/api/sitian/report")

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["summary"] == "通用频道项目已分析"
    assert report["projects"] == [
        {
            "projectId": str(workspace),
            "projectName": "generic-workspace",
            "statusByRule": "active",
            "statusReason": "recent generic chat activity",
            "narrative": "会话包含待处理的通用频道工作。",
            "sessionStats": {
                "total": 1,
                "activeWithin1h": 1,
                "activeWithin24h": 1,
                "activeWithin3d": 1,
            },
            "alertIds": [],
        }
    ]
