"""map_reduce mapper prompt 构造器。

本脚本负责把 workflow 目标、输出契约、shard 信息和物化输入清单转换为中文 mapper prompt。
作用是约束 mapper 子 agent 只分析当前 shard，只输出 `code_findings` JSON，并为每条 finding 提供文件、行号、证据和建议。
关键执行流程：调用 MapperPromptBuilder.build，生成包含任务目标、文件映射、字段契约和 JSON 示例的 prompt。
关键函数：MapperPromptBuilder.build 生成 prompt，MapperPromptBuilder.build_from_spec 从 workflow spec 提取目标和契约后生成 prompt。
"""

from __future__ import annotations

import json
from typing import Literal

from executors.agent_runtime.strategies.map_reduce_contracts import (
    MapperInputManifest,
    MapReduceWorkflowSpec,
    MapShard,
)
from executors.agent_runtime.subagent_permissions import to_jsonable


class MapperPromptBuilder:
    """为单个 map_reduce shard 生成中文 mapper prompt。"""

    def build_from_spec(
        self,
        *,
        spec: MapReduceWorkflowSpec,
        shard: MapShard,
        manifest: MapperInputManifest,
    ) -> str:
        """从 workflow spec 生成 prompt，输入为 spec、shard 和 manifest，输出为中文 prompt。"""
        return self.build(
            objective=spec.objective,
            output_contract=spec.output_contract,
            mapper_prompt_template=spec.mapper.prompt_template,
            shard=shard,
            manifest=manifest,
        )

    def build(
        self,
        *,
        objective: str,
        output_contract: Literal["code_findings", "raw_text"] | str,
        mapper_prompt_template: str,
        shard: MapShard,
        manifest: MapperInputManifest,
    ) -> str:
        """生成 mapper prompt，输入为目标、输出契约、shard 和 manifest，输出为严格 JSON 指令。"""
        if output_contract == "raw_text":
            return _raw_text_prompt(
                objective=objective,
                mapper_prompt_template=mapper_prompt_template,
                shard=shard,
                manifest=manifest,
            )
        if output_contract != "code_findings":
            raise ValueError(
                "MapperPromptBuilder only supports output_contract='code_findings' or 'raw_text'"
            )
        file_rows = [
            {
                "original_path": item.original_path,
                "materialized_path": item.materialized_path,
                "content_digest": item.content_digest,
                "truncated": item.truncated,
                "truncation_reason": item.truncation_reason,
            }
            for item in manifest.files
        ]
        return "\n".join(
            [
                "你是 map_reduce mapper 子 agent，只分析当前 shard。",
                "",
                "任务目标：",
                objective.strip(),
                "",
                "shard 信息：",
                _json_block(
                    {
                        "shard_id": shard.shard_id,
                        "shard_name": shard.shard_name,
                        "display_order": shard.display_order,
                        "module_hint": shard.module_hint,
                        "shard_reason": shard.shard_reason,
                        "estimated_tokens": shard.estimated_tokens,
                        "shard_digest": shard.shard_digest,
                        "context": shard.context,
                    }
                ),
                "",
                "输入文件映射：",
                _json_block(
                    {
                        "task_run_id": manifest.task_run_id,
                        "input_dir": manifest.input_dir,
                        "files": file_rows,
                    }
                ),
                "",
                "执行要求：",
                "- 只读取输入文件映射中的 materialized_path。",
                "- 在 files_seen 中写 original_path，locations.path 也使用 original_path。",
                "- 每条 finding 必须至少包含一个 locations 条目，locations.line_start 和 locations.line_end 必须是原始文件行号。",
                "- evidence 必须包含可核验的文件路径和行号，excerpt 使用短摘录。",
                "- 文件被截断时，把无法确认的风险写入 errors 或 coverage.skipped_files。",
                "- 没有发现问题时，findings 输出空数组，并完整填写 coverage。",
                "",
                "只输出一个 JSON 对象，禁止输出 Markdown、解释文字或代码块。",
                "JSON 顶层结构必须满足 code_findings 契约：",
                _json_block(_example_payload(shard=shard, manifest=manifest)),
            ]
        ).strip()


def _example_payload(*, shard: MapShard, manifest: MapperInputManifest) -> dict[str, object]:
    """构造 JSON 示例，输入为 shard 和 manifest，输出为 code_findings 示例 payload。"""
    first_file = manifest.files[0].original_path if manifest.files else "path/to/file.py"
    return {
        "output_contract": "code_findings",
        "shard_id": shard.shard_id,
        "status": "completed",
        "summary": "用一句中文概括本 shard 的检查结果。",
        "files_seen": [first_file],
        "findings": [
            {
                "dedupe_key": f"{shard.shard_id}:{first_file}:10:示例问题标题",
                "title": "示例问题标题",
                "category": "bug",
                "severity": "P1",
                "confidence": 0.8,
                "locations": [
                    {
                        "path": first_file,
                        "line_start": 10,
                        "line_end": 12,
                        "symbol": "ExampleSymbol",
                        "excerpt": "短证据摘录，保留关键代码。",
                    }
                ],
                "evidence": f"{first_file}:10-12 显示该问题的直接证据。",
                "rationale": "说明为什么这是问题。",
                "recommendation": "给出可执行修复建议。",
                "impact_area": ["runtime"],
                "source_shard_id": shard.shard_id,
            }
        ],
        "coverage": {
            "files_assigned": len(manifest.files),
            "files_seen_count": 1 if manifest.files else 0,
            "symbols_seen_count": 0,
            "skipped_files": [],
            "skip_reasons": [],
        },
        "errors": [
            {
                "error_type": "tool_error",
                "message": "只在真实发生错误时填写。",
                "file_path": first_file,
                "retryable": True,
            }
        ],
    }


def _raw_text_prompt(
    *,
    objective: str,
    mapper_prompt_template: str,
    shard: MapShard,
    manifest: MapperInputManifest,
) -> str:
    """生成 raw_text mapper prompt，输入为目标和用户模板，输出为自由文本任务提示。"""
    file_rows = [
        {
            "original_path": item.original_path,
            "materialized_path": item.materialized_path,
            "content_digest": item.content_digest,
        }
        for item in manifest.files
    ]
    return "\n".join(
        [
            "你是 map_reduce mapper 子 agent，只完成当前 shard 的任务。",
            "",
            "任务目标：",
            objective.strip(),
            "",
            "shard 信息：",
            _json_block(
                {
                    "shard_id": shard.shard_id,
                    "shard_name": shard.shard_name,
                    "display_order": shard.display_order,
                    "module_hint": shard.module_hint,
                    "shard_reason": shard.shard_reason,
                    "context": shard.context,
                }
            ),
            "",
            "输入文件映射：",
            _json_block(
                {
                    "task_run_id": manifest.task_run_id,
                    "input_dir": manifest.input_dir,
                    "files": file_rows,
                }
            ),
            "",
            "mapper 任务：",
            mapper_prompt_template.strip(),
            "",
            "输出要求：",
            "- 直接输出 mapper 任务要求的最终文本。",
            "- 保持简洁，禁止输出 Markdown 代码块。",
        ]
    ).strip()


def _json_block(payload: object) -> str:
    """序列化 prompt 内 JSON 片段，输入为 payload，输出为缩进 JSON 字符串。"""
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, default=str)


__all__ = ["MapperPromptBuilder"]
