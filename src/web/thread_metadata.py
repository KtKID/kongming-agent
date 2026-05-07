"""Thread 元数据持久化层。

每个 thread 在 ``.kongming/web/threads/<thread_id>/metadata.json`` 落一份
:class:`ThreadMetadata` 文件。本文件提供：

- :class:`ThreadMetadata` Pydantic 模型（当前 schema_version=6，v0.2.3 起）
- :func:`thread_metadata_path` —— 路径常量
- :func:`write_thread_metadata` —— 原子写入（``tmp.replace(path)``）
- :func:`read_thread_metadata` —— 读 + 校验；schema_version 不匹配 / JSON
  损坏返回 ``None`` 并记 warning
- :func:`list_thread_metadata` —— 扫盘（用于启动时列出所有 thread）
- :func:`delete_thread_metadata_dir` —— 整目录删除（含 metadata.json 同级
  的其他 cell 元数据，未来 v0.1.6+ 可能新增）

设计要点：

- :class:`ThreadMetadata` 与 :class:`web.protocol.ThreadMetadataDTO`
  字段一致，两者**刻意分离**——前者是落盘用的内部模型，后者是 REST 出
  口的对外契约。v0.1.5 字段相同，但语义不同：DTO 可能未来加 stripping
  字段（隐藏内部字段），模型保留全集。
- ``frozen=True``：metadata 一经构造不可变；想改字段就重建 + 重写盘。
- 损坏文件不抛 —— 返回 None，由调用方决定是否跳过 / 删除 / 重建。
- 不依赖 web.protocol：metadata 是后端本地状态，前端只通过 REST DTO 看到。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


# v0.2.3 schema 版本：bump 到 6 以支持 provider thread id 命名统一。
# ``claude_thread_id`` / ``codex_thread_id`` 都表示 provider 底层可恢复 thread id。
# 一个 Kongming thread 绑定一个 provider session/thread；老 v5 文件读入时把
# ``sdk_session_id`` 迁移为 ``claude_thread_id``。
THREAD_METADATA_SCHEMA_VERSION = 6


class ThreadMetadata(BaseModel):
    """单个 thread 的持久化元数据。

    落盘形态：``.kongming/web/threads/<thread_id>/metadata.json``。

    Attributes:
        id: thread ID，格式 ``thread-<hex12>``。与 session_id 同值。
        name: 用户给 thread 起的名（最长 200 字符）。
        preset_id: 创建时选的 LLM preset ID（来自
            :class:`config_loader.models.LLMPresetConfig`）。``backend_kind="claude_code"``
            时允许空字符串占位（claude_code 由 SDK 内部选 model，不需要 preset）。
        backend_kind: 后端类型；``"generic_chat"`` 表示走 InputAssembler + LLM provider 的
            原有路径；``"claude_code"`` 表示走 ``/ws/claude-code``+ Claude Agent SDK。
            v0.1.6 新增；老 v1 文件兼容默认 ``"generic_chat"``。
        claude_thread_id: Claude 底层 thread/session id。一个 Kongming thread 绑定
            一个 Claude session；空字符串表示未绑定（首次对话前 / 仅 generic_chat 后端）。
            一旦绑定即记录 Claude 侧分配的可恢复 id，用于下次 resume 历史。
        codex_thread_id: Codex 底层 thread/session id。一个 Kongming thread 绑定
            一个 Codex session；空字符串表示未绑定或非 codex 后端。
        cwd: claude_code 后端运行时的工作目录绝对路径。v0.2.0 新增；用于定位
            ``~/.claude/projects/<encoded-cwd>/<claude_thread_id>.jsonl`` 历史文件。
            空字符串表示**不需要 / 未设置**（generic_chat 后端不消费此字段）。
        created_at: Unix 时间戳（秒）。
        updated_at: Unix 时间戳（秒）；rename / 一轮对话结束时更新。
        message_count: 历史消息总数；用于 UI 上的"X 条消息"展示。
        cumulative_prompt_tokens: thread 级累计输入 token 总量；每次 run 结束后
            加上本次 ``Result.metadata.usage.prompt_tokens``。
        cumulative_completion_tokens: thread 级累计输出 token 总量。
        cumulative_total_tokens: thread 级累计总 token。
        schema_version: 当前 ``6``；``Literal[1, 2, 3, 4, 5, 6]`` 同时接受老文件，
            写盘时永远写 ``6``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    preset_id: str = ""
    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    claude_thread_id: str = ""
    codex_thread_id: str = ""
    cwd: str = ""
    created_at: float
    updated_at: float
    message_count: Annotated[int, Field(ge=0)] = 0
    cumulative_prompt_tokens: Annotated[int, Field(ge=0)] = 0
    cumulative_completion_tokens: Annotated[int, Field(ge=0)] = 0
    cumulative_total_tokens: Annotated[int, Field(ge=0)] = 0
    schema_version: Literal[1, 2, 3, 4, 5, 6] = 6


def thread_metadata_path(home: Path, thread_id: str) -> Path:
    """返回单个 thread 的 metadata.json 路径。

    Args:
        home: ``.kongming/`` 根目录（由 :func:`config_loader.get_kongming_home`
            提供）。
        thread_id: ``thread-<hex12>`` 格式 ID。

    Returns:
        ``<home>/web/threads/<thread_id>/metadata.json`` 的 :class:`Path`。
        不保证文件存在。
    """
    return home / "web" / "threads" / thread_id / "metadata.json"


def thread_metadata_dir(home: Path, thread_id: str) -> Path:
    """返回单个 thread 的目录路径（含 metadata.json 同级文件预留）。"""
    return home / "web" / "threads" / thread_id


def write_thread_metadata(home: Path, meta: ThreadMetadata) -> None:
    """原子写入 metadata.json。

    实现：先写 ``metadata.json.tmp``，再 ``os.replace`` 到目标路径，
    避免半写文件造成读盘解析失败。

    Args:
        home: ``.kongming/`` 根目录。
        meta: 要持久化的 :class:`ThreadMetadata` 实例。

    Raises:
        OSError: 父目录创建失败 / 写文件失败。让调用方决定 retry / 报错。
    """
    path = thread_metadata_path(home, meta.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    # os.replace 在 POSIX / Windows 都是原子的（同一文件系统内）。
    os.replace(tmp, path)


def read_thread_metadata(home: Path, thread_id: str) -> ThreadMetadata | None:
    """读盘 + 校验单个 thread 的 metadata.json。

    返回 ``None`` 的几种情况：

    - 文件不存在 / 不是普通文件
    - JSON 解析失败（损坏 / 编码异常）
    - schema_version 不在 ``{1, 2, 3, 4, 5, 6}``（更高版本 = 该进程不认识，拒绝）
    - 字段校验失败（缺字段 / 类型不对 / 正则不匹配）

    **v1 → v2 懒升级**：``schema_version=1`` 且缺 ``backend_kind`` 时，
    自动在内存里补 ``backend_kind="generic_chat"`` 与 ``schema_version=2``。

    **v2 → v3 懒升级**：``schema_version=2`` 且缺 ``sdk_session_id`` 时，
    自动在内存里补 ``sdk_session_id=""`` / ``cwd=""`` 与 ``schema_version=3``。

    **v3 → v4 懒升级**：``schema_version=3`` 且缺累计 usage 字段时，
    自动在内存里补 3 个累计字段与 ``schema_version=4``。

    **v4 → v5 懒升级**：``schema_version=4`` 且缺 ``codex_thread_id`` 时，
    自动在内存里补 ``codex_thread_id=""`` 与 ``schema_version=5``。

    **v5 → v6 懒升级**：把旧 ``sdk_session_id`` 迁移到 ``claude_thread_id``，
    并删除旧字段，避免 ``extra="forbid"`` 校验失败。

    返回的 :class:`ThreadMetadata` 实例已是最新 v6 形态。下次
    :func:`write_thread_metadata` 会以 v6 写盘（默认 ``schema_version=6``，
    无需调用方关心）。本函数**不**自己回写——避免读盘函数有副作用。

    所有 ``None`` 路径都会记 warning 日志，便于排查。
    本函数永不抛 —— 让调用方在 list_thread_metadata 里安全跳过损坏项。
    """
    path = thread_metadata_path(home, thread_id)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to read thread metadata at %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("thread metadata JSON corrupted at %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("thread metadata not a JSON object at %s", path)
        return None
    # v1 → v2 懒升级：老文件 schema_version=1，缺 backend_kind 字段
    if data.get("schema_version") == 1 and "backend_kind" not in data:
        data["backend_kind"] = "generic_chat"
        # 升级到 v2，让下游模型直接验证为新版本
        data["schema_version"] = 2
    # v2 → v3 懒升级：缺旧 sdk_session_id 字段（v0.2.0 claude-code-history-resume 新增）
    if data.get("schema_version") == 2 and "sdk_session_id" not in data:
        data["sdk_session_id"] = ""
        data["cwd"] = ""
        data["schema_version"] = 3
    # v3 → v4 懒升级：补累计 usage 字段（v0.2.1 thread 级 token 持久化新增）
    if data.get("schema_version") == 3:
        data.setdefault("cumulative_prompt_tokens", 0)
        data.setdefault("cumulative_completion_tokens", 0)
        data.setdefault("cumulative_total_tokens", 0)
        data["schema_version"] = 4
    # v4 → v5 懒升级：补 codex_thread_id 字段（v0.2.2 codex 接入新增）
    if data.get("schema_version") == 4:
        data.setdefault("codex_thread_id", "")
        data["schema_version"] = 5
    # v5 → v6 懒升级：旧 sdk_session_id 字段改名为 claude_thread_id
    if data.get("schema_version") == 5:
        data["claude_thread_id"] = str(data.pop("sdk_session_id", data.get("claude_thread_id", "")))
        data.setdefault("codex_thread_id", "")
        data["schema_version"] = 6
    # 兜底：任何 version 下如果 sdk_session_id 仍残留，强制迁移
    if "sdk_session_id" in data:
        data.setdefault("claude_thread_id", str(data.pop("sdk_session_id")))
    try:
        return ThreadMetadata.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "thread metadata validation failed at %s: %s",
            path,
            exc,
        )
        return None


def list_thread_metadata(home: Path) -> list[ThreadMetadata]:
    """扫盘列出所有可读取的 thread metadata。

    实现：遍历 ``<home>/web/threads/`` 下所有目录，挨个读 metadata.json。

    返回顺序：按 ``updated_at`` 降序（最近活跃排前面）。

    损坏 / 缺失 metadata 的目录直接跳过（read_thread_metadata 返回 None）。
    若 root 目录本身不存在，返回 ``[]``。

    本函数是同步 IO，调用方按需用 ``asyncio.to_thread`` 隔离事件循环。
    """
    root = home / "web" / "threads"
    if not root.is_dir():
        return []
    out: list[ThreadMetadata] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        thread_id = child.name
        meta = read_thread_metadata(home, thread_id)
        if meta is None:
            continue
        # 容错：万一目录名与 metadata.id 不一致，按 metadata 为准（不重命名目录）
        out.append(meta)
    out.sort(key=lambda m: m.updated_at, reverse=True)
    return out


def delete_thread_metadata_dir(home: Path, thread_id: str) -> None:
    """删除单个 thread 的整目录（含 metadata.json 及未来扩展文件）。

    幂等：目录不存在时静默跳过。
    """
    target = thread_metadata_dir(home, thread_id)
    if not target.is_dir():
        return
    # 不用 shutil.rmtree（避免误删符号链接外的内容）；逐文件清理后 rmdir
    for child in target.iterdir():
        if child.is_file() or child.is_symlink():
            try:
                child.unlink()
            except OSError as exc:
                logger.warning("failed to delete %s: %s", child, exc)
        elif child.is_dir():
            # v0.1.5 不预期 metadata 目录里再有子目录；保险处理
            for sub in child.iterdir():
                try:
                    sub.unlink()
                except OSError as exc:
                    logger.warning("failed to delete nested %s: %s", sub, exc)
            try:
                child.rmdir()
            except OSError as exc:
                logger.warning("failed to rmdir %s: %s", child, exc)
    try:
        target.rmdir()
    except OSError as exc:
        logger.warning("failed to rmdir %s: %s", target, exc)


__all__ = [
    "THREAD_METADATA_SCHEMA_VERSION",
    "ThreadMetadata",
    "delete_thread_metadata_dir",
    "list_thread_metadata",
    "read_thread_metadata",
    "thread_metadata_dir",
    "thread_metadata_path",
    "write_thread_metadata",
]
