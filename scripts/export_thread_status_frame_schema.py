"""导出 Web thread-status 帧的 Pydantic serialization JSON Schema。

功能与流程：
1. 从 ``hosts.web.protocol`` 公共入口读取 delta 与 snapshot 真源模型。
2. 使用 Pydantic serialization mode 生成 JSON Schema。
3. 按后端 ``model_dump(exclude_none=True)`` 的真实输出规则补齐必填字段。
4. 以稳定 UTF-8/LF 格式写入 Web 协议生成目录。

关键函数：
- ``build_thread_status_frame_schema``：构造并规范化 delta schema。
- ``build_thread_status_snapshot_frame_schema``：构造并规范化 snapshot schema。
- ``export_thread_status_frame_schemas``：把两个 schema 写入指定路径。
- ``main``：解析命令行参数并执行导出。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticUndefined

from hosts.web.protocol import ThreadStatusFrame, ThreadStatusSnapshotFrame

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    REPO_ROOT / "web" / "src" / "protocol" / "generated" / "thread-status-frame.schema.json"
)
DEFAULT_SNAPSHOT_OUTPUT = (
    REPO_ROOT
    / "web"
    / "src"
    / "protocol"
    / "generated"
    / "thread-status-snapshot-frame.schema.json"
)


def _serialization_required_fields(
    schema: dict[str, Any],
    model: type[ThreadStatusFrame] | type[ThreadStatusSnapshotFrame],
) -> list[str]:
    """计算 ``exclude_none=True`` 输出中始终存在的字段，输入为模型 schema，输出为字段名列表。"""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("ThreadStatusFrame schema 缺少 properties")

    required = {item for item in schema.get("required", []) if isinstance(item, str)}
    for name, field in model.model_fields.items():
        if field.default is not PydanticUndefined and field.default is not None:
            required.add(name)
    return [name for name in properties if name in required]


def build_thread_status_frame_schema() -> dict[str, Any]:
    """从 Pydantic 模型构造确定性 serialization schema，输入为空，输出为 JSON 可序列化字典。"""
    schema = TypeAdapter(ThreadStatusFrame).json_schema(mode="serialization")
    schema["required"] = _serialization_required_fields(schema, ThreadStatusFrame)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("ThreadStatusFrame schema 缺少 properties")
    for property_schema in properties.values():
        if isinstance(property_schema, dict):
            property_schema.pop("title", None)
    schema["$comment"] = (
        "由 scripts/export_thread_status_frame_schema.py 自动生成；"
        "禁止手工修改。required 与 model_dump(exclude_none=True) 对齐。"
    )
    return schema


def build_thread_status_snapshot_frame_schema() -> dict[str, Any]:
    """从 Pydantic 模型构造 snapshot serialization schema。"""
    schema = TypeAdapter(ThreadStatusSnapshotFrame).json_schema(mode="serialization")
    schema["required"] = _serialization_required_fields(
        schema,
        ThreadStatusSnapshotFrame,
    )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("ThreadStatusSnapshotFrame schema 缺少 properties")
    for property_schema in properties.values():
        if isinstance(property_schema, dict):
            property_schema.pop("title", None)
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        item_schema = definitions.get("ThreadStatusFrame")
        if isinstance(item_schema, dict):
            item_schema["required"] = _serialization_required_fields(
                item_schema,
                ThreadStatusFrame,
            )
    schema["$comment"] = (
        "由 scripts/export_thread_status_frame_schema.py 自动生成；"
        "禁止手工修改。snapshot 与 Python wire 模型保持一致。"
    )
    return schema


def _write_schema(output: Path, schema: dict[str, Any]) -> None:
    """把一个 schema 稳定写入目标路径。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(
        schema,
        ensure_ascii=False,
        indent=2,
    )
    output.write_text(f"{rendered}\n", encoding="utf-8", newline="\n")


def export_thread_status_frame_schema(output: Path) -> None:
    """把 delta schema 写入 output。"""
    _write_schema(output, build_thread_status_frame_schema())


def export_thread_status_frame_schemas(
    output: Path,
    snapshot_output: Path,
) -> None:
    """把 delta 与 snapshot schema 同步写入各自目标路径。"""
    _write_schema(output, build_thread_status_frame_schema())
    _write_schema(snapshot_output, build_thread_status_snapshot_frame_schema())


def _parse_args() -> argparse.Namespace:
    """解析 schema 输出路径，输入来自命令行，输出为 argparse 参数对象。"""
    parser = argparse.ArgumentParser(
        description="导出 ThreadStatusFrame Pydantic serialization schema",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON Schema 输出路径",
    )
    parser.add_argument(
        "--snapshot-output",
        type=Path,
        default=DEFAULT_SNAPSHOT_OUTPUT,
        help="Snapshot JSON Schema 输出路径",
    )
    return parser.parse_args()


def main() -> None:
    """执行命令行导出，输入为命令行参数，输出为生成的 schema 文件。"""
    args = _parse_args()
    export_thread_status_frame_schemas(
        args.output.resolve(),
        args.snapshot_output.resolve(),
    )


if __name__ == "__main__":
    main()
