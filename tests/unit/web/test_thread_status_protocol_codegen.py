"""ThreadStatusFrame Pydantic → JSON Schema → TypeScript 生成合同测试。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_thread_status_frame_schema import (
    DEFAULT_OUTPUT,
    DEFAULT_SNAPSHOT_OUTPUT,
    build_thread_status_frame_schema,
    build_thread_status_snapshot_frame_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TYPESCRIPT_OUTPUT = REPO_ROOT / "web" / "src" / "protocol" / "generated" / "thread-status-frame.ts"
SNAPSHOT_TYPESCRIPT_OUTPUT = (
    REPO_ROOT / "web" / "src" / "protocol" / "generated" / "thread-status-snapshot-frame.ts"
)
HANDWRITTEN_PROTOCOL = REPO_ROOT / "web" / "src" / "protocol" / "ws-thread-status.ts"


def test_thread_status_schema_matches_pydantic_export() -> None:
    """已提交 schema 必须与当前 Pydantic 真源逐字段一致。"""
    committed = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))

    assert committed == build_thread_status_frame_schema()
    assert committed["required"] == [
        "frame_type",
        "threadId",
        "phase",
        "sequence",
        "runId",
        "runGeneration",
    ]
    assert list(committed["properties"]) == [
        "frame_type",
        "threadId",
        "phase",
        "sequence",
        "runId",
        "runGeneration",
        "toolName",
        "run_end_reason",
    ]
    assert committed["additionalProperties"] is False


def test_thread_status_snapshot_schema_matches_pydantic_export() -> None:
    """Snapshot 也必须由 Python 模型生成，避免手写 TS 外壳漂移。"""
    committed = json.loads(DEFAULT_SNAPSHOT_OUTPUT.read_text(encoding="utf-8"))

    assert committed == build_thread_status_snapshot_frame_schema()
    assert committed["required"] == ["frame_type", "watermark", "items"]
    assert list(committed["properties"]) == ["frame_type", "watermark", "items"]
    assert committed["additionalProperties"] is False


def test_generated_typescript_owns_thread_status_frame() -> None:
    """生成文件持有完整 interface，手写协议文件只负责重导出和组合 union。"""
    generated = TYPESCRIPT_OUTPUT.read_text(encoding="utf-8")
    snapshot_generated = SNAPSHOT_TYPESCRIPT_OUTPUT.read_text(encoding="utf-8")
    handwritten = HANDWRITTEN_PROTOCOL.read_text(encoding="utf-8")

    assert "export interface ThreadStatusFrame" in generated
    assert "run_end_reason?: number | null;" in generated
    assert "runEndReason" not in generated
    assert "export interface ThreadStatusFrame" not in handwritten
    assert "export interface ThreadStatusSnapshotFrame" in snapshot_generated
    assert "watermark: number;" in snapshot_generated
    assert "items: ThreadStatusFrame[]" in snapshot_generated
    assert "export interface ThreadStatusSnapshotFrame" not in handwritten
