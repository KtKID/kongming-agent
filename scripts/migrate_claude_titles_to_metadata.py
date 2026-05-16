#!/usr/bin/env python3
"""把 Claude session jsonl 里历史 ``custom-title`` / ``archived`` 事件回填到
thread ``metadata.json``。

背景：``claude-session-rename-archive-metadata-source`` 把 rename / archive
真源从 jsonl 事件迁到 ``ThreadMetadata.{name,is_archived}``。新逻辑只读
metadata.json，不再扫 jsonl 尾部。老用户已经 rename / archive 过的 session 必
须一次性迁移，否则旧标题与归档状态会"丢失"——其实仍在 jsonl 里，只是新
scanner 不读。

实现要点：

- **反向逐行扫描整文件**：不依赖旧 4KB 尾部窗口（fad8497 / 0452e8f 实现的
  ``read_custom_title`` / ``read_archived``）——单条 message 中位数 ~7KB，
  P99 达 285KB，4KB 尾窗口经常错过 rename 事件，这正是 bug 本身。我们改成
  从文件尾部 64KB 一块块往回读，按 ``\\n`` 切行，逐行 ``json.loads`` 找
  最后一条 ``custom-title`` / ``archived`` 记录。最坏全文件扫一遍，依然比
  正向快——因为目标事件通常在尾部。
- **跳过 ``agent-*.jsonl``**：subagent 工具历史，不参与 session 列表。
- **保护人工已改名**：``meta.name`` 与 ``os.path.basename(meta.cwd)`` 相等时
  才视作默认名（首次 import session 时的占位），可以被 jsonl 里的
  custom-title 覆盖；否则保持不动。
- **archived 无脑覆盖 True**：只在 jsonl 里找到 ``archived:true`` 才动；
  ``archived:false`` 不触发覆盖（保持原值）。归档没有"反归档"的事件序列
  语义，按"最后一次归档动作"算就够了。
- **``--dry-run``**：只统计不写盘；用户可以先看报告再决定。

用法::

    python scripts/migrate_claude_titles_to_metadata.py [--dry-run] \\
        [--claude-home PATH] [--kongming-home PATH]

退出码：

- 0：迁移完成（无论是否有命中）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# 让脚本无需安装 wheel 即可跑（开发期 src layout）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from web.thread_metadata import (  # noqa: E402
    ThreadMetadata,
    list_thread_metadata,
    write_thread_metadata,
)

# 反向扫描块大小：64KB。够大以容纳 P99 单行（~285KB 也只需 5 次扩块），
# 又不至于一上来吃整个文件。
_REVERSE_READ_BLOCK_SIZE = 64 * 1024


@dataclass
class MigrationStats:
    """统计计数器。"""

    scanned_jsonl: int = 0
    """扫描的 jsonl 文件总数（含 skip 的）。"""

    skipped_unbound: int = 0
    """未绑定 thread（找不到 metadata）→ skip。"""

    has_custom_title: int = 0
    """jsonl 中找到 ``custom-title`` 事件的数量。"""

    migrated_title: int = 0
    """命中"默认名 → custom-title"覆盖的数量。"""

    skipped_already_named: int = 0
    """人工已改名跳过的数量。"""

    migrated_polluted: int = 0
    """metadata.name 被首条 user msg 污染 → 用 jsonl custom-title 覆盖的数量。"""

    migrated_archived: int = 0
    """``archived:true`` 事件触发 is_archived=True 覆盖的数量。"""

    no_event: int = 0
    """没有任何 custom-title / archived 事件 → skip。"""

    updated_threads: set[str] = field(default_factory=set)
    """实际有变更的 thread id（用于 dry-run 时区分"会写"与"实际写了"）。"""


def _iter_lines_reversed(path: Path) -> Iterator[str]:
    """反向逐行迭代器：从文件尾部往前 yield 每一行（不含行尾 ``\\n``）。

    实现：按 64KB 块从尾部往前读到 ``buffer``，按 ``\\n`` 切分；最后一块的
    第一段可能是半截行（因为块边界切在某行中间），保留到下一块的尾部再拼。
    跨编码错位的字节用 ``errors="replace"`` 兜底。

    空文件直接返回空迭代器（不 yield）。打开失败抛 ``OSError`` 让调用方处理。
    """
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        remaining = fh.tell()
        leftover = b""  # 上一轮块开头那段半截行（属于更靠后位置）

        while remaining > 0:
            read_size = min(_REVERSE_READ_BLOCK_SIZE, remaining)
            remaining -= read_size
            fh.seek(remaining)
            chunk = fh.read(read_size)
            buffer = chunk + leftover
            # 按 \n 切行
            parts = buffer.split(b"\n")
            # 第一段如果还有 remaining > 0，可能是半截行 → 留到下一轮
            if remaining > 0:
                leftover = parts[0]
                parts = parts[1:]
            else:
                leftover = b""
            # 反向 yield：尾部的行先出
            for line_bytes in reversed(parts):
                if not line_bytes:
                    continue
                yield line_bytes.decode("utf-8", errors="replace")

        # 文件首块如果有残留（说明开头那行不带前导 \n，但只要 remaining=0 进入
        # 上面分支时 leftover 已置空，这里实际不会有内容；保留分支防御）。
        if leftover:
            yield leftover.decode("utf-8", errors="replace")


def find_custom_title_and_archived(
    jsonl_path: Path,
) -> tuple[str | None, bool]:
    """反向扫整个 jsonl，找最后一条 ``custom-title`` 和最后一条
    ``archived: true``。

    返回 ``(custom_title_or_None, archived_true_flag)``。

    - ``custom_title``：最后一条 ``{"type":"custom-title","customTitle":"..."}``
      的 ``customTitle`` 值。
    - ``archived_true_flag``：是否找到过 ``{"type":"archived","archived":true}``。
      只要有一条 True 就 True；``archived:false`` 不触发。

    任一打不开 / 解析失败 → 返回 ``(None, False)``。
    """
    custom_title: str | None = None
    archived_true = False
    try:
        for line in _iter_lines_reversed(jsonl_path):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if (
                custom_title is None
                and etype == "custom-title"
                and isinstance(entry.get("customTitle"), str)
            ):
                custom_title = str(entry["customTitle"])
            elif not archived_true and etype == "archived" and entry.get("archived") is True:
                archived_true = True
            # 两个都找到了就提前退出
            if custom_title is not None and archived_true:
                break
    except OSError:
        return (None, False)
    return (custom_title, archived_true)


_TITLE_MAX_LEN = 40


def first_user_message_title(jsonl_path: Path) -> str | None:
    """正向扫 jsonl 取第一条 ``type=user`` 且 ``message.content`` 是 string 的截断。

    跟 ``projects_scanner._extract_title`` / ``_build_session_summary`` 的 fallback
    语义一致：``message.content`` → ``replace("\\n", " ").strip()[:_TITLE_MAX_LEN]``。
    找不到合格 entry → ``None``。

    用途：识别 metadata.name 是否被"首条 user msg"污染（独立于 custom-title 写入路径
    的另一条历史 bug）。脚本据此放宽 default-name 判定。
    """
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "user":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                cleaned = content.replace("\n", " ").strip()
                if not cleaned:
                    continue
                return cleaned[:_TITLE_MAX_LEN]
    except OSError:
        return None
    return None


def iter_jsonl_files(claude_home: Path) -> Iterator[Path]:
    """遍历 ``<claude_home>/projects/*/<uuid>.jsonl``。

    跳过 ``agent-*.jsonl``（subagent 工具历史）。``claude_home/projects/`` 不
    存在时直接返回空。
    """
    root = claude_home / "projects"
    if not root.is_dir():
        return
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.name.startswith("agent-"):
                continue
            yield jsonl


def migrate(
    *,
    claude_home: Path,
    kongming_home: Path,
    dry_run: bool,
) -> MigrationStats:
    """主入口：扫 claude_home 下的 jsonl，回填到 kongming_home 的 metadata。

    Args:
        claude_home: Claude SDK 的 ``~/.claude`` 等价目录。
        kongming_home: kongming-agent 的 ``.kongming`` 等价目录。
        dry_run: True → 只统计不写盘。

    Returns:
        :class:`MigrationStats` 实例，调用方负责打印 / 处理。
    """
    stats = MigrationStats()

    # 构建 claude_thread_id → ThreadMetadata 索引。
    # ``list_thread_metadata`` 返回按 ``(is_pinned, updated_at) DESC`` 排序的列表，
    # 历史上 ``bind_claude_thread`` 的 1:1 守门可能漏掉过若干路径（导致同一
    # ``claude_thread_id`` 挂多个 thread metadata）。这里**保留首次出现**——即
    # 排序最优（置顶 + 最近活跃）的那条，避免被后者（次优）覆盖。
    metas = list_thread_metadata(kongming_home)
    index: dict[str, ThreadMetadata] = {}
    for meta in metas:
        if meta.claude_thread_id:
            index.setdefault(meta.claude_thread_id, meta)

    for jsonl_path in iter_jsonl_files(claude_home):
        stats.scanned_jsonl += 1
        claude_thread_id = jsonl_path.stem
        meta = index.get(claude_thread_id)
        if meta is None:
            stats.skipped_unbound += 1
            continue

        custom_title, archived_true = find_custom_title_and_archived(jsonl_path)

        if custom_title is None and not archived_true:
            stats.no_event += 1
            continue

        new_name = meta.name
        new_archived = meta.is_archived

        if custom_title is not None:
            stats.has_custom_title += 1
            default_name = os.path.basename(meta.cwd) if meta.cwd else ""
            is_default = meta.name == default_name and default_name != ""
            # 第二条规则：识别 metadata.name 被首条 user msg 污染的历史 bug
            polluted_match: str | None = None
            if not is_default:
                polluted_match = first_user_message_title(jsonl_path)
            is_polluted = polluted_match is not None and meta.name == polluted_match
            if is_default:
                new_name = custom_title
                stats.migrated_title += 1
            elif is_polluted:
                new_name = custom_title
                stats.migrated_polluted += 1
            else:
                stats.skipped_already_named += 1

        if archived_true:
            # 无脑覆盖 True；如果已经是 True，仍然算"已迁移"一次
            if not meta.is_archived:
                new_archived = True
                stats.migrated_archived += 1
            else:
                # 已经是 True 不再计；但用户也可能想知道"jsonl 里有 archived 事件"
                # 不做单独统计，保持报告字段简单
                pass

        if new_name != meta.name or new_archived != meta.is_archived:
            updated = meta.model_copy(update={"name": new_name, "is_archived": new_archived})
            stats.updated_threads.add(meta.id)
            if not dry_run:
                write_thread_metadata(kongming_home, updated)

    return stats


def render_report(stats: MigrationStats, *, dry_run: bool) -> str:
    """格式化迁移报告字符串。"""
    lines = [
        "迁移报告",
        "========",
        f"扫描 jsonl 总数：{stats.scanned_jsonl}",
        f"未绑定 thread (skip)：{stats.skipped_unbound}",
        f"有 custom-title 事件：{stats.has_custom_title}",
        f"  - 命中覆盖（默认名 → custom-title）：{stats.migrated_title}",
        f"  - 命中覆盖（污染：首条 user msg → custom-title）：{stats.migrated_polluted}",
        f"  - 跳过（人工已改名）：{stats.skipped_already_named}",
        f"有 archived 事件（已覆盖 is_archived=True）：{stats.migrated_archived}",
        f"无任何事件：{stats.no_event}",
    ]
    if dry_run:
        lines.append("")
        lines.append("DRY RUN: 未实际写盘")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "把 Claude session jsonl 里历史 custom-title / archived 事件迁移到 "
            "thread metadata.json。新 scanner 不再读 jsonl 尾部，老用户必须运行"
            "一次以保留历史标题 / 归档状态。"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描 + 报告，不写盘（推荐先用一次确认命中数量）",
    )
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=None,
        help="Claude SDK 主目录（默认 ~/.claude）",
    )
    parser.add_argument(
        "--kongming-home",
        type=Path,
        default=None,
        help="kongming-agent 主目录（默认 $KONGMING_HOME 或 ./.kongming）",
    )
    return parser.parse_args(argv)


def _resolve_claude_home(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser().resolve()
    return (Path.home() / ".claude").resolve()


def _resolve_kongming_home(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser().resolve()
    # 复用项目运行时入口
    from config_loader.paths import get_kongming_home

    return get_kongming_home()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    claude_home = _resolve_claude_home(args.claude_home)
    kongming_home = _resolve_kongming_home(args.kongming_home)
    stats = migrate(
        claude_home=claude_home,
        kongming_home=kongming_home,
        dry_run=args.dry_run,
    )
    print(render_report(stats, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
