"""roundtable_review 共享白板与输入物化。

本脚本负责写入 ReviewBoard 产物，并把被评审源码复制到每个子 agent 的 scoped workdir。
作用是让独立评审、交叉质询和仲裁阶段共享同一份上下文、来源、claims 和 rebuttals。
关键执行流程：收集输入文件，写 context.md/sources.md，把源文件与 board snapshot 复制到子
agent workdir，再持续追加 claims.jsonl/rebuttals.jsonl 和最终报告。
关键函数：collect_source_files 收集输入文件，materialize_for_task 复制子 agent 输入，
ReviewBoardWriter 写入共享白板文件。
"""

from __future__ import annotations

import fnmatch
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from application.agent_workflows.strategies.roundtable_review.contracts import (
    ReviewClaimRecord,
    ReviewCommentRecord,
    RoundtableReviewSpec,
    SourceFileRecord,
)
from application.agent_workflows.task_models import SubAgentTask
from application.subagents.permissions import to_jsonable


@dataclass(frozen=True)
class ReviewBoardPaths:
    # ReviewBoard 根目录。
    board_dir: Path
    # 评审上下文。
    context_path: Path
    # 来源清单。
    sources_path: Path
    # claim JSONL。
    claims_path: Path
    # rebuttal JSONL。
    rebuttals_path: Path
    # 共识文档。
    consensus_path: Path
    # 最终报告。
    final_report_path: Path


class ReviewBoardWriter:
    """写入 workflow_dir/review_board 下的圆桌评审白板。"""

    def __init__(self, *, workflow_dir: Path) -> None:
        """初始化白板 writer，输入为 workflow 目录，输出为绑定白板目录的实例。"""
        self._workflow_dir = workflow_dir.expanduser().resolve()

    @property
    def paths(self) -> ReviewBoardPaths:
        """返回全部白板路径，输入为空，输出为 ReviewBoardPaths。"""
        board_dir = self._workflow_dir / "review_board"
        return ReviewBoardPaths(
            board_dir=board_dir,
            context_path=board_dir / "context.md",
            sources_path=board_dir / "sources.md",
            claims_path=board_dir / "claims.jsonl",
            rebuttals_path=board_dir / "rebuttals.jsonl",
            consensus_path=board_dir / "consensus.md",
            final_report_path=board_dir / "final_report.md",
        )

    def write_context(self, *, spec: RoundtableReviewSpec, workflow_id: str) -> Path:
        """写入上下文文档，输入为 spec 和 workflow_id，输出为 context.md 路径。"""
        lines = [
            "# Multi-Agent Roundtable Review Context",
            "",
            f"- workflow_id: {workflow_id}",
            f"- topic: {spec.topic}",
            f"- objective: {spec.objective}",
            f"- discussion_rounds: {spec.limits.discussion_rounds}",
            f"- max_discussion_rounds: {spec.limits.max_discussion_rounds}",
            f"- total_child_token_budget: {spec.limits.total_child_token_budget}",
            "",
            "## Reviewers",
            "",
        ]
        for reviewer in spec.reviewers:
            lines.append(f"- {reviewer.agent_id}: {reviewer.focus}")
        lines.extend(
            [
                "",
                "## Output Contract",
                "",
                "Independent reviewers output JSON: agent, findings[].",
                "Cross-question reviewers output JSON: agent, comments[].",
                "Every claim or comment must bind concrete evidence.",
                "",
            ]
        )
        return self._write_text(self.paths.context_path, "\n".join(lines))

    def write_sources(self, files: tuple[SourceFileRecord, ...]) -> Path:
        """写入来源清单，输入为源文件记录，输出为 sources.md 路径。"""
        lines = ["# Sources", "", f"- source_file_count: {len(files)}", ""]
        for item in files:
            suffix = " truncated" if item.truncated else ""
            lines.append(
                f"- {item.path} -> {item.materialized_path} ({item.size_bytes} bytes{suffix})"
            )
        lines.append("")
        return self._write_text(self.paths.sources_path, "\n".join(lines))

    def append_claims(self, claims: tuple[ReviewClaimRecord, ...]) -> None:
        """追加 claim 记录，输入为 claims，输出为 claims.jsonl 新内容。"""
        self._append_jsonl(self.paths.claims_path, claims)

    def append_comments(self, comments: tuple[ReviewCommentRecord, ...]) -> None:
        """追加 rebuttal/comment 记录，输入为 comments，输出为 rebuttals.jsonl 新内容。"""
        self._append_jsonl(self.paths.rebuttals_path, comments)

    def write_consensus(
        self,
        *,
        claims: tuple[ReviewClaimRecord, ...],
        comments: tuple[ReviewCommentRecord, ...],
    ) -> Path:
        """写入确定性共识草稿，输入为 claim/comment，输出为 consensus.md 路径。"""
        support_count: dict[str, int] = {}
        refute_count: dict[str, int] = {}
        for comment in comments:
            if comment.comment_type == "support":
                support_count[comment.target_claim_id] = (
                    support_count.get(comment.target_claim_id, 0) + 1
                )
            elif comment.comment_type == "refute":
                refute_count[comment.target_claim_id] = (
                    refute_count.get(comment.target_claim_id, 0) + 1
                )

        consensus = [
            claim
            for claim in claims
            if support_count.get(claim.claim_id, 0) >= refute_count.get(claim.claim_id, 0)
        ]
        disputed = [
            claim
            for claim in claims
            if refute_count.get(claim.claim_id, 0) > support_count.get(claim.claim_id, 0)
        ]
        lines = ["# Consensus Draft", "", "## 共识候选", ""]
        for claim in consensus:
            lines.append(
                f"- {claim.claim_id} [{claim.severity}] {claim.claim} "
                f"(support={support_count.get(claim.claim_id, 0)}, refute={refute_count.get(claim.claim_id, 0)})"
            )
        lines.extend(["", "## 主要分歧", ""])
        for claim in disputed:
            lines.append(
                f"- {claim.claim_id} [{claim.severity}] {claim.claim} "
                f"(support={support_count.get(claim.claim_id, 0)}, refute={refute_count.get(claim.claim_id, 0)})"
            )
        lines.append("")
        return self._write_text(self.paths.consensus_path, "\n".join(lines))

    def write_final_report(self, content: str) -> Path:
        """写入最终报告，输入为报告正文，输出为 final_report.md 路径。"""
        return self._write_text(self.paths.final_report_path, content.strip() + "\n")

    def snapshot_text(
        self,
        *,
        claims: tuple[ReviewClaimRecord, ...],
        comments: tuple[ReviewCommentRecord, ...],
    ) -> str:
        """生成白板快照，输入为当前 claim/comment，输出为可放进子 agent 上下文的文本。"""
        parts = [
            self.paths.context_path.read_text(encoding="utf-8")
            if self.paths.context_path.exists()
            else "",
            self.paths.sources_path.read_text(encoding="utf-8")
            if self.paths.sources_path.exists()
            else "",
            "# Claims",
            json.dumps([to_jsonable(claim) for claim in claims], ensure_ascii=False, indent=2),
            "# Rebuttals",
            json.dumps(
                [to_jsonable(comment) for comment in comments], ensure_ascii=False, indent=2
            ),
        ]
        return "\n\n".join(part for part in parts if part)

    def _append_jsonl(self, path: Path, rows: tuple[Any, ...]) -> None:
        """追加 JSONL，输入为路径和记录序列，输出为文件新行。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(to_jsonable(row), ensure_ascii=False, default=str) + "\n")

    def _write_text(self, path: Path, content: str) -> Path:
        """原子写入文本，输入为路径和内容，输出为路径。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return path


def collect_source_files(
    *,
    workspace_root: Path,
    spec: RoundtableReviewSpec,
) -> tuple[Path, tuple[SourceFileRecord, ...], tuple[Path, ...]]:
    """收集评审源文件，输入为 workspace 和 spec，输出为 input_root、记录和绝对路径。"""
    input_root = _resolve_input_root(workspace_root, spec.input_source.root_dir)
    files: list[Path] = []
    for raw_path in spec.input_source.paths:
        files.extend(_expand_path(input_root, raw_path))
    for pattern in spec.input_source.include:
        files.extend(path for path in input_root.glob(pattern) if path.is_file())
    unique = _dedupe_paths(files)
    filtered = [
        path
        for path in unique
        if _is_relative_to(path, input_root)
        and not _excluded(path.relative_to(input_root).as_posix(), spec.input_source.exclude)
    ]
    limited = tuple(filtered[: spec.input_source.max_files])
    records = tuple(
        SourceFileRecord(
            path=path.relative_to(input_root).as_posix(),
            materialized_path=f"input/source/{path.relative_to(input_root).as_posix()}",
            size_bytes=path.stat().st_size,
            truncated=path.stat().st_size > spec.input_source.max_bytes_per_file,
        )
        for path in limited
    )
    return input_root, records, limited


def materialize_for_task(
    *,
    task: SubAgentTask,
    input_root: Path,
    source_paths: tuple[Path, ...],
    source_records: tuple[SourceFileRecord, ...],
    board_snapshot: str,
    max_bytes_per_file: int,
) -> Path:
    """为子 agent 复制输入，输入为任务和文件列表，输出为 manifest 路径。"""
    working_dir_raw = task.metadata.get("working_dir")
    if not isinstance(working_dir_raw, str) or not working_dir_raw.strip():
        raise ValueError("roundtable_review task requires working_dir")
    working_dir = Path(working_dir_raw).resolve()
    input_dir = working_dir / "input"
    source_dir = input_dir / "source"
    input_dir.mkdir(parents=True, exist_ok=True)
    for src, record in zip(source_paths, source_records, strict=True):
        dest = source_dir / record.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_limited(src, dest, max_bytes=max_bytes_per_file)
    snapshot_path = input_dir / "review_board_snapshot.md"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(board_snapshot, encoding="utf-8")
    manifest_path = input_dir / "input_manifest.json"
    manifest = {
        "written_at": _now_iso(),
        "input_root": str(input_root),
        "source_file_count": len(source_records),
        "source_files": [to_jsonable(record) for record in source_records],
        "review_board_snapshot": "input/review_board_snapshot.md",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _resolve_input_root(workspace_root: Path, root_dir: str) -> Path:
    """解析输入根目录，输入为 workspace 和 root_dir，输出为绝对路径。"""
    root = Path(root_dir).expanduser()
    if not root.is_absolute():
        root = workspace_root / root
    resolved = root.resolve()
    if not _is_relative_to(resolved, workspace_root.resolve()):
        raise ValueError(f"roundtable_review input root must stay inside workspace: {resolved}")
    return resolved


def _expand_path(input_root: Path, raw_path: str) -> list[Path]:
    """展开路径，输入为 input_root 和用户路径，输出为文件列表。"""
    candidate = (input_root / raw_path).resolve()
    if not _is_relative_to(candidate, input_root):
        raise ValueError(f"roundtable_review input path escapes input root: {raw_path}")
    if candidate.is_file():
        return [candidate]
    if candidate.is_dir():
        return sorted(path for path in candidate.rglob("*") if path.is_file())
    return sorted(path for path in input_root.glob(raw_path) if path.is_file())


def _copy_limited(src: Path, dest: Path, *, max_bytes: int) -> None:
    """复制文件并按字节截断，输入为源/目标路径，输出为目标文件。"""
    if src.stat().st_size <= max_bytes:
        shutil.copyfile(src, dest)
        return
    with open(src, "rb") as handle:
        data = handle.read(max_bytes)
    dest.write_bytes(data + b"\n\n[roundtable_review: file truncated]\n")


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    """按绝对路径去重，输入为路径列表，输出为稳定排序路径。"""
    seen: set[str] = set()
    output: list[Path] = []
    for path in sorted(path.resolve() for path in paths):
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(path)
    return output


def _excluded(path: str, patterns: tuple[str, ...]) -> bool:
    """判断路径是否命中排除规则，输入为相对路径和 glob，输出为布尔值。"""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断路径是否位于 root 内，输入为两个路径，输出为布尔值。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _now_iso() -> str:
    """生成当前 UTC 时间，输入为空，输出为 ISO 字符串。"""
    return datetime.now(UTC).isoformat()


__all__ = [
    "ReviewBoardPaths",
    "ReviewBoardWriter",
    "collect_source_files",
    "materialize_for_task",
]
