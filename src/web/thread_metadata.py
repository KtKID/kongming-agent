"""Thread 元数据持久化层。

每个 thread 在 ``.kongming/web/threads/<thread_id>/metadata.json`` 落一份
:class:`ThreadMetadata` 文件。本文件提供：

- :class:`ThreadMetadata` Pydantic 模型（当前 schema_version=8，task#2 起）
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


# schema 版本演进：
# - v7（v0.2.4）：bump 到 7 以支持 thread 置顶功能（is_pinned 字段）。
# - **v8（usage-token-manager-core task#2）**：删 5 个 ``cumulative_*_tokens`` 字段
#   （prompt / completion / total / cache_read / cache_creation），改为嵌套
#   ``cumulative_usage: dict[str, Any] | None``——透明 dict 落盘，**仅
#   ``UsageTokenManager`` 能解释**。
#
# ``claude_thread_id`` / ``codex_thread_id`` 都表示 provider 底层可恢复 thread id。
# 一个 Kongming thread 绑定一个 provider session/thread；老 v5 文件读入时把
# ``sdk_session_id`` 迁移为 ``claude_thread_id``。
THREAD_METADATA_SCHEMA_VERSION = 9


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
        cumulative_usage: token 用量累计（v8 新增，**透明 dict 落盘**）。

            ⚠️ **架构约束**：本字段是透明 dict，**仅 ``UsageTokenManager`` 能解释**。
            外部模块禁止直接读字段；通过 ``manager.get_thread_summary(thread_id)``
            拿派生 summary。

            落盘格式（Anthropic 系，``backend_kind=claude_code`` 或 generic_chat-anthropic）::

                {
                  "channel": "anthropic",
                  "input_tokens": N,                  # 不含 cache 的纯新输入累计
                  "cache_read_input_tokens": N,       # 命中 cache 输入累计（独立计数）
                  "cache_creation_input_tokens": N,   # 写 cache 输入累计（独立计数）
                  "output_tokens": N                  # 输出累计
                }

            落盘格式（OpenAI 系，``backend_kind=codex`` 或 generic_chat-openai）::

                {
                  "channel": "openai",
                  "input_tokens": N,                  # 总输入（含 cached_input 子集）
                  "cached_input_tokens": N,           # 命中 cache 子集（是 input 子集）
                  "output_tokens": N,                 # 总输出（含 reasoning 子集）
                  "reasoning_output_tokens": N        # 推理思考用量（是 output 子集）
                }

            ``None`` 表示该 thread 还没跑过任何 turn。
        is_pinned: 置顶标记；``True`` 时 UI 列表优先排列。v0.2.4 新增。
        schema_version: 当前 ``8``；``Literal[1, ..., 8]`` 同时接受老文件，
            写盘时永远写 ``8``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(pattern=r"^thread-[a-f0-9]{12}$")]
    """thread 唯一标识，格式 ``thread-<hex12>``；与 runner session_id 同值。"""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    """thread 显示名（用户输入，最长 200 字符；UI 列表展示用）。"""

    preset_id: str = ""
    """创建时选的 LLM preset ID（claude_code 允许空字符串占位）。"""

    backend_kind: Literal["generic_chat", "claude_code", "codex"] = "generic_chat"
    """后端通道类型（transport 维度，与 token 语义 ``channel`` 解耦）。"""

    claude_thread_id: str = ""
    """Claude SDK 底层 session id（仅 claude_code 通道有效）。"""

    codex_thread_id: str = ""
    """Codex CLI 底层 session id（仅 codex 通道有效）。"""

    cwd: str = ""
    """claude_code 通道工作目录绝对路径（定位 transcript jsonl）。"""

    created_at: float
    """thread 创建时间（Unix 时间戳，秒）。"""

    updated_at: float
    """最近活跃时间（Unix 时间戳，秒）。"""

    message_count: Annotated[int, Field(ge=0)] = 0
    """历史消息总数（UI 列表展示 "X 条消息"）。"""

    # ⚠️ v9（usage-token-v2-bigbang）**物理删除** 3 个 token 字段：
    #   - cumulative_usage / last_run_snapshot / last_model_name
    # token 真源回归 SDK 写的 jsonl/rollout，由 UsageTokenManager v2 现场派生。
    # 旧 v8 文件含这 3 字段读入时由 read_thread_metadata 的 v8→v9 lazy upgrade
    # 自动 drop。``extra="forbid"`` 在 v9 下严格拒绝任何残留写入。
    # 详见 docs/usage-token-v2/04-data-and-state.md §metadata schema v9。

    is_pinned: bool = False
    """是否置顶（v0.2.4 新增）。"""

    schema_version: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9] = 9
    """schema 版本号（当前 v9，usage-token-v2 bigbang 引入）。
    ``Literal[1..9]`` 接受所有历史文件，写盘永远写 9。"""


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
    - schema_version 不在 ``{1, ..., 9}``（更高版本 = 该进程不认识，拒绝）
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

    **v6 → v7 懒升级**：补 ``is_pinned`` 字段（v0.2.4 置顶功能新增）。

    **v7 → v8 懒升级**（usage-token-manager-core task#2）：
    删旧 5 个 ``cumulative_*_tokens`` 字段，按 ``backend_kind`` 映射到嵌套
    ``cumulative_usage: dict``（``upgrade_v7_to_v8`` 实现）。
    旧 ``cumulative_total_tokens`` 是派生量，直接丢弃。

    **v8 → v9 懒升级**（usage-token-v2-bigbang）：
    **drop** 3 个 token 字段：``cumulative_usage`` / ``last_run_snapshot``
    / ``last_model_name``。token 真源回归 SDK 写的 jsonl/rollout，由
    ``UsageTokenManager v2`` 现场派生，metadata.json 不再缓存。
    旧数据**不迁移**（真源在 SDK，旧字段是冗余拷贝）。

    返回的 :class:`ThreadMetadata` 实例已是最新 v9 形态。下次
    :func:`write_thread_metadata` 会以 v9 写盘（默认 ``schema_version=9``，
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
    # v6 → v7 懒升级：补 is_pinned 字段（v0.2.4 置顶功能新增）
    if data.get("schema_version") == 6:
        data.setdefault("is_pinned", False)
        data["schema_version"] = 7
    # v7 → v8 懒升级：drop 旧 5 个 cumulative_*_tokens 字段（v9 不再保留 token
    # 字段，所以 v7→v8 不再构造 cumulative_usage——直接 drop 后续 v8→v9 也 drop）。
    if data.get("schema_version") == 7:
        for key in (
            "cumulative_prompt_tokens",
            "cumulative_completion_tokens",
            "cumulative_total_tokens",
            "cumulative_cache_read_tokens",
            "cumulative_cache_creation_tokens",
        ):
            data.pop(key, None)
        data["schema_version"] = 8
    # v8 → v9 懒升级（usage-token-v2-bigbang）：drop 3 个 token 字段。
    # token 真源回归 SDK 写的 jsonl/rollout，由 UsageTokenManager v2 现场派生。
    # 旧数据不迁移（真源在 SDK，旧字段是冗余拷贝）。
    if data.get("schema_version") == 8:
        data.pop("cumulative_usage", None)
        data.pop("last_run_snapshot", None)
        data.pop("last_model_name", None)
        data["schema_version"] = 9
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
    # 先按 is_pinned 降序（置顶排前），再按 updated_at 降序
    out.sort(key=lambda m: (m.is_pinned, m.updated_at), reverse=True)
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
