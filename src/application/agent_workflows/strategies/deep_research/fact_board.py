"""Deep Research artifact writer。

本脚本负责把 Deep Research 各阶段结构化产物写入 workflow 目录。
作用是固定 sources、facts、groups、rulings、stats 和 report 的落盘路径，供 result 和 viewer 后续消费。
关键执行流程：初始化 deep_research 目录，逐类写 JSONL/JSON/Markdown，返回 artifact path 索引。
关键函数：DeepResearchArtifactWriter.write_sources/write_facts/write_groups/write_rulings/write_stats/write_report。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class DeepResearchArtifactWriter:
    """写入 Deep Research artifact。"""

    def __init__(self, workflow_dir: Path) -> None:
        """初始化 writer，输入为 workflow 目录，输出为绑定 deep_research 目录的 writer。"""
        self.root = workflow_dir / "deep_research"
        self.root.mkdir(parents=True, exist_ok=True)
        spec_path = self.root / "spec.json"
        if not spec_path.exists():
            self._write_json(spec_path, {})

    def write_plan(self, payload: Mapping[str, object]) -> Path:
        """写入 plan JSON，输入为 plan payload，输出为 plan.json 路径。"""
        path = self.root / "plan.json"
        self._write_json(path, dict(payload))
        return path

    def append_source(self, payload: Mapping[str, object], *, bucket: str) -> Path:
        """追加来源记录，输入为来源 payload 和 bucket，输出为 JSONL 路径。"""
        return self._append_jsonl(f"sources.{bucket}.jsonl", payload)

    def append_fact(self, payload: Mapping[str, object], *, bucket: str) -> Path:
        """追加事实记录，输入为事实 payload 和 bucket，输出为 JSONL 路径。"""
        return self._append_jsonl(f"facts.{bucket}.jsonl", payload)

    def append_group(self, payload: Mapping[str, object]) -> Path:
        """追加事实组，输入为 group payload，输出为 groups.jsonl 路径。"""
        return self._append_jsonl("groups.jsonl", payload)

    def append_ruling(self, payload: Mapping[str, object]) -> Path:
        """追加 jury ruling，输入为 ruling payload，输出为 rulings.jsonl 路径。"""
        return self._append_jsonl("rulings.jsonl", payload)

    def append_checked_group(self, payload: Mapping[str, object]) -> Path:
        """追加已裁决事实组，输入为 checked group payload，输出为 groups.checked.jsonl 路径。"""
        return self._append_jsonl("groups.checked.jsonl", payload)

    def append_phase_summary(self, payload: Mapping[str, object]) -> Path:
        """追加阶段摘要，输入为 summary payload，输出为 phase_summaries.jsonl 路径。"""
        return self._append_jsonl("phase_summaries.jsonl", payload)

    def write_sources(self, records: Sequence[object]) -> Path:
        """写入来源 JSONL，输入为来源记录，输出为 sources.jsonl 路径。"""
        return self._write_jsonl("sources.jsonl", records)

    def write_facts(self, records: Sequence[object]) -> Path:
        """写入事实 JSONL，输入为事实记录，输出为 facts.jsonl 路径。"""
        return self._write_jsonl("facts.jsonl", records)

    def write_groups(self, records: Sequence[object]) -> Path:
        """写入事实组 JSONL，输入为事实组记录，输出为 groups.jsonl 路径。"""
        return self._write_jsonl("groups.jsonl", records)

    def write_rulings(self, records: Sequence[object]) -> Path:
        """写入裁决 JSONL，输入为裁决记录，输出为 rulings.jsonl 路径。"""
        return self._write_jsonl("rulings.jsonl", records)

    def write_stats(self, payload: Mapping[str, object]) -> Path:
        """写入统计 JSON，输入为统计映射，输出为 stats.json 路径。"""
        path = self.root / "stats.json"
        self._write_json(path, dict(payload))
        return path

    def write_report(
        self,
        payload: Mapping[str, object] | str,
        *,
        markdown: str | None = None,
    ) -> Path:
        """写入报告 Markdown，输入为报告正文，输出为 report.md 路径。"""
        if isinstance(payload, Mapping):
            self._write_json(self.root / "report.json", dict(payload))
            content = markdown or ""
        else:
            content = payload
        path = self.root / "report.md"
        path.write_text(content, encoding="utf-8")
        return path

    def artifact_paths(self) -> dict[str, str]:
        """返回 artifact 路径索引，输入为当前目录，输出为稳定 path 字典。"""
        return {
            "root": str(self.root),
            "sources_path": str(self.root / "sources.jsonl"),
            "facts_path": str(self.root / "facts.jsonl"),
            "groups_path": str(self.root / "groups.jsonl"),
            "rulings_path": str(self.root / "rulings.jsonl"),
            "stats_path": str(self.root / "stats.json"),
            "report_path": str(self.root / "report.md"),
        }

    def _write_jsonl(self, name: str, records: Sequence[object]) -> Path:
        """写入 JSONL，输入为文件名和记录序列，输出为目标路径。"""
        path = self.root / name
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(_to_jsonable(record), ensure_ascii=False, default=str))
                handle.write("\n")
        return path

    def _append_jsonl(self, name: str, payload: Mapping[str, object]) -> Path:
        """追加 JSONL，输入为文件名和 payload，输出为目标路径。"""
        path = self.root / name
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_jsonable(dict(payload)), ensure_ascii=False, default=str))
            handle.write("\n")
        return path

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        """原子写入 JSON，输入为路径和 payload，输出为文件更新。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


def _to_jsonable(value: object) -> Any:
    """转换 JSON 友好结构，输入为任意值，输出为 dict/list/标量。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = ["DeepResearchArtifactWriter"]
