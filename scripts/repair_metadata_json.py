#!/usr/bin/env python3
"""修复被并发写盘竞争损坏的 thread metadata.json 文件。

背景：``usage-token-derive-from-jsonl-v0.1`` 之前的 ``fix-last-snapshot-persist``
引入了 fire-and-forget 写盘，跟 ``record_run_usage`` 的 lock 内写盘竞争，导致部分
``metadata.json`` 文件出现"两次 JSON 拼一起"的尾巴垃圾，``json.loads`` 抛 ``Extra data``。

修复方案：用 ``json.JSONDecoder().raw_decode`` 拿到第一个合法 JSON 的结束位置，
截断尾巴垃圾，原子写回。截断前合法部分如果本身能成功解析为 ``ThreadMetadata``
schema，那么截断后即可恢复——不会丢失任何字段。

用法::

    # 默认 dry-run（只报损坏文件清单，不动盘）
    uv run python scripts/repair_metadata_json.py

    # 真改
    uv run python scripts/repair_metadata_json.py --apply

    # 自定义扫描目录
    uv run python scripts/repair_metadata_json.py --root /path/to/.kongming

退出码：

- 0：没有损坏文件，或 dry-run 找到了但没改
- 0：``--apply`` 模式全部修复成功
- 1：``--apply`` 模式有文件修复失败
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def find_metadata_files(root: Path) -> Iterable[Path]:
    """扫描 ``<root>/web/threads/*/metadata.json``。"""
    threads_dir = root / "web" / "threads"
    if not threads_dir.is_dir():
        return
    for child in threads_dir.iterdir():
        if not child.is_dir():
            continue
        p = child / "metadata.json"
        if p.is_file():
            yield p


def detect_corruption(raw: str) -> tuple[bool, str | None, int]:
    """检测文件是否损坏，并返回合法 JSON 的结束位置（用于截断修复）。

    Returns:
        (is_corrupted, error_message, valid_end_pos)
        - is_corrupted=False：文件没问题
        - is_corrupted=True：error_message 有内容，valid_end_pos 是第一段合法
          JSON 在 raw 中的结束 char 位置（用于 ``raw[:valid_end_pos]`` 截断）
    """
    try:
        json.loads(raw)
        return (False, None, len(raw))
    except json.JSONDecodeError as exc:
        # raw_decode 拿第一段合法 JSON 的结束位置
        try:
            _, end = json.JSONDecoder().raw_decode(raw)
            return (True, str(exc), end)
        except json.JSONDecodeError as inner_exc:
            # 整个文件第一段都解不出来——无可修复
            return (True, f"unrecoverable: {inner_exc}", -1)


def atomic_write(path: Path, content: str) -> None:
    """原子写入：先写 ``.tmp.<pid>`` 再 ``os.replace``。

    用唯一 tmp 后缀（PID）避免并发写撞共享 tmp 文件——本脚本是单进程，主要为防
    并发跑或跟运行中的服务撞。
    """
    tmp = path.with_name(f"{path.name}.repair.tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".kongming"),
        help="kongming home 目录（默认 ``.kongming``）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真改盘（不加默认 dry-run 模式，只报告）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印每个文件的检查结果（默认只打印损坏文件）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    root: Path = args.root.resolve()
    if not root.exists():
        logger.error("root not found: %s", root)
        return 1

    files = list(find_metadata_files(root))
    logger.info("scanning %d metadata files under %s", len(files), root)

    corrupted: list[tuple[Path, str, int]] = []
    for p in files:
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("skip %s: %s", p.parent.name, exc)
            continue
        is_corrupted, err, end = detect_corruption(raw)
        if is_corrupted:
            corrupted.append((p, err or "", end))
            logger.warning(
                "CORRUPT %s (%d bytes, valid up to %d): %s",
                p.parent.name,
                len(raw),
                end,
                err,
            )
        elif args.verbose:
            logger.debug("ok %s", p.parent.name)

    if not corrupted:
        logger.info("no corrupted files; all metadata.json parse OK")
        return 0

    logger.info("found %d corrupted file(s)", len(corrupted))

    if not args.apply:
        logger.info("dry-run mode; pass --apply to repair")
        return 0

    fixed = 0
    failed = 0
    for p, err, end in corrupted:
        if end <= 0:
            logger.error("UNRECOVERABLE %s: %s (skipped)", p.parent.name, err)
            failed += 1
            continue
        try:
            raw = p.read_text(encoding="utf-8")
            truncated = raw[:end]
            # 双重校验：截断后必须能 json.loads
            json.loads(truncated)
            # 确保末尾有换行（pydantic model_dump_json 不带尾换行；写盘风格统一）
            if not truncated.endswith("\n"):
                truncated += "\n"
            atomic_write(p, truncated)
            logger.info("FIXED %s (%d → %d bytes)", p.parent.name, len(raw), len(truncated))
            fixed += 1
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("FAIL %s: %s", p.parent.name, exc)
            failed += 1

    logger.info("repair done: fixed=%d failed=%d", fixed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
