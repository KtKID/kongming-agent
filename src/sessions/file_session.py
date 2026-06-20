"""FileSession — append-only JSONL 文件持久化 session。

:class:`FileSession` 是 :class:`core.contracts.Session` 协议的文件持久化实现：
每次 ``append()`` 将消息以 JSONL 行追加到磁盘，支持进程重启后恢复续写。

存储结构::

    <store_path>/<session_id>/
      manifest.json
      system_prompt.json
      <session_id>.jsonl

设计决策（v0.1.2）：
- 延迟 materialize：``__init__`` 不创建文件，首次 ``append()`` 才触发落盘
- 无 pending_buffer：未 materialize 时 ``history()`` 返回空列表
- 消息链：``message_id`` (UUIDv4) + ``parent_message_id``，无 seq 字段
- 每条 append 后 flush + fsync
- ``system_prompt.json`` 会持久化完整组装后的 system prompt，供本地审计
  和问题复盘使用；该文件应按 session 数据同等级别保护。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from core.message import Message

# FileSession 通过结构化协议（duck typing）满足 Session Protocol，
# 不显式继承，与 SQLiteSession 风格一致。
from sessions.session_bootstrap import SessionBootstrap
from sessions.session_store import _message_from_dict, _message_to_dict

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "0.1.2"


class FileSession:
    """Append-only JSONL 文件持久化 session。

    Args:
        session_id: 会话唯一标识。
        bootstrap: CLI 阶段产出的稳定元数据。
        store_path: session 目录的父目录（如 ``.kongming/sessions``）。
    """

    def __init__(
        self,
        session_id: str,
        bootstrap: SessionBootstrap,
        store_path: str,
    ) -> None:
        self.session_id = session_id
        self._bootstrap = bootstrap
        self._store_path = Path(store_path)

        self._session_dir = self._store_path / session_id
        self._manifest_path = self._session_dir / "manifest.json"
        self._system_prompt_path = self._session_dir / "system_prompt.json"
        self._messages_path = self._session_dir / f"{session_id}.jsonl"

        self._materialized: bool = False
        self._materialize_lock = RLock()
        self._last_message_id: str | None = None
        # run_count 在 manifest.json 中持久化；advance_run_index 时 +1 + 写盘
        self._run_count: int = 0

        # 检测已存在目录 → 走恢复路径
        if self._session_dir.is_dir() and self._manifest_path.is_file():
            self._recover()

    # ------------------------------------------------------------------
    # Session Protocol
    # ------------------------------------------------------------------

    async def append(self, message: Message, *, usage: dict[str, Any] | None = None) -> None:
        """追加一条消息。首次调用触发 materialize。"""
        if not self._materialized:
            self._materialize()

        message_id = str(uuid.uuid4())
        parent_message_id = self._last_message_id

        record: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "record_type": "message",
            "session_id": self.session_id,
            "model_name": self._bootstrap.model_name,
            "message_id": message_id,
            "parent_message_id": parent_message_id,
            "created_at": time.time(),
            "message": _message_to_dict(message),
        }
        if usage is not None:
            record["usage"] = dict(usage)

        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self._messages_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

        self._last_message_id = message_id

    async def history(self) -> list[Message]:
        """读取所有历史消息。未 materialize 时返回空列表。"""
        if not self._materialized:
            return []

        messages: list[Message] = []
        with open(self._messages_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("record_type", "message") != "message":
                        continue
                    messages.append(_message_from_dict(record["message"]))
                except (json.JSONDecodeError, KeyError, TypeError):
                    logger.warning(
                        "session %s: 跳过损坏行 #%d in %s",
                        self.session_id,
                        line_no,
                        self._messages_path,
                    )

        return messages

    async def clear(self) -> None:
        """清空 session 文件，重置为未 materialize 状态。"""
        with self._materialize_lock:
            if self._session_dir.is_dir():
                for child in self._session_dir.iterdir():
                    child.unlink()
                self._session_dir.rmdir()

            self._materialized = False
            self._last_message_id = None
            self._run_count = 0

    async def advance_run_index(self) -> int:
        """递增 run_count 并立即写回 manifest.json，返回新值。

        非事务原子约定见 :meth:`core.contracts.Session.advance_run_index`：
        本方法只保证"内存 +1 + manifest 落盘"两步完成；与上游 ``append`` 不
        构成单事务，崩溃模式下不会让 run_id 重复或丢消息。

        若当前未 materialize（极罕见，理论上 runner 总是先 append 再 advance），
        先 _materialize 把目录和空 jsonl 准备好，再写 manifest。
        """
        if not self._materialized:
            self._materialize()
        self._run_count += 1
        self._write_manifest()
        return self._run_count

    # ------------------------------------------------------------------
    # materialize / recover
    # ------------------------------------------------------------------

    def _materialize(self) -> None:
        """创建目录、原子写 manifest、准备 jsonl 文件。"""
        with self._materialize_lock:
            if self._materialized:
                return
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._write_manifest()
            self._write_system_prompt_snapshot()

            # 创建空 jsonl（如果不存在）
            if not self._messages_path.exists():
                self._messages_path.touch()

            self._materialized = True

    def _write_manifest(self) -> None:
        """原子写 manifest.json：先写 ``.tmp`` 再 rename，含 fsync。

        被 :meth:`_materialize`（首次落盘）和 :meth:`advance_run_index`（更新
        ``run_count``）共用；任何会变更 manifest 内容的地方都应走这条路径。
        """
        manifest: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": self._bootstrap.created_at,
            "agent_name": self._bootstrap.agent_name,
            "model_name": self._bootstrap.model_name,
            "instruction_sources": self._bootstrap.instruction_sources,
            "instruction_text_hash": self._bootstrap.instruction_text_hash,
            "cwd": self._bootstrap.cwd,
            "app_version": self._bootstrap.app_version,
            "format": f"{self.session_id}.jsonl",
            "run_count": self._run_count,
        }

        tmp_path = self._manifest_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._manifest_path)

    def _write_system_prompt_snapshot(self) -> None:
        """把最终 system prompt 写入 thread 级快照文件。"""
        if self._bootstrap.instruction_text is None:
            return

        snapshot: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "record_type": "system_prompt",
            "session_id": self.session_id,
            "model_name": self._bootstrap.model_name,
            "created_at": self._bootstrap.created_at,
            "instruction_sources": self._bootstrap.instruction_sources,
            "instruction_text_hash": self._bootstrap.instruction_text_hash,
            "cwd": self._bootstrap.cwd,
            "app_version": self._bootstrap.app_version,
            "content": self._bootstrap.instruction_text,
        }

        tmp_path = self._system_prompt_path.with_name(
            f"{self._system_prompt_path.name}.{uuid.uuid4().hex}.tmp"
        )
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._system_prompt_path)

    def _recover(self) -> None:
        """从磁盘恢复 session 状态。"""
        # 恢复 bootstrap（从 manifest 读取，但保留传入的 bootstrap 以保持一致性）
        try:
            with open(self._manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
            # 更新 bootstrap 为 manifest 中保存的版本（恢复场景）
            self._bootstrap = SessionBootstrap(
                agent_name=manifest.get("agent_name", self._bootstrap.agent_name),
                model_name=manifest.get("model_name", self._bootstrap.model_name),
                instruction_sources=manifest.get(
                    "instruction_sources", self._bootstrap.instruction_sources
                ),
                instruction_text_hash=manifest.get(
                    "instruction_text_hash", self._bootstrap.instruction_text_hash
                ),
                created_at=manifest.get("created_at", self._bootstrap.created_at),
                cwd=manifest.get("cwd", self._bootstrap.cwd),
                instruction_text=self._bootstrap.instruction_text,
                app_version=manifest.get("app_version", self._bootstrap.app_version),
            )
            # 旧 manifest 无 run_count 字段时兜底为 0；用户已声明旧 session 全删，
            # 这里仅防御开发期残留旧文件触发 KeyError。
            self._run_count = int(manifest.get("run_count", 0))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "session %s: manifest 损坏，使用传入 bootstrap: %s", self.session_id, exc
            )

        # 从 jsonl 恢复 last_message_id
        if self._messages_path.exists():
            last_id: str | None = None
            with open(self._messages_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("record_type", "message") != "message":
                            continue
                        last_id = record.get("message_id")
                    except (json.JSONDecodeError, KeyError):
                        continue
            self._last_message_id = last_id

        self._materialized = True

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    def validate(self) -> ValidationResult:
        """校验消息链完整性。返回校验结果，不打断正常流程。"""
        if not self._materialized:
            return ValidationResult(valid=True, errors=[])

        errors: list[str] = []
        seen_ids: set[str] = set()
        prev_id: str | None = None

        with open(self._messages_path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    errors.append(f"行 {line_no}: JSON 解析失败")
                    continue
                if record.get("record_type", "message") != "message":
                    continue

                msg_id = record.get("message_id")
                parent_id = record.get("parent_message_id")

                if msg_id is None:
                    errors.append(f"行 {line_no}: 缺少 message_id")
                    continue

                if msg_id in seen_ids:
                    errors.append(f"行 {line_no}: 重复 message_id={msg_id}")
                    continue
                seen_ids.add(msg_id)

                if prev_id is None:
                    if parent_id is not None:
                        errors.append(f"行 {line_no}: 首条消息 parent_message_id 应为 null")
                else:
                    if parent_id != prev_id:
                        errors.append(
                            f"行 {line_no}: parent_message_id={parent_id} 不等于前条 message_id={prev_id}"
                        )

                prev_id = msg_id

        return ValidationResult(valid=len(errors) == 0, errors=errors)


class ValidationResult:
    """校验结果。"""

    def __init__(self, valid: bool, errors: list[str]) -> None:
        self.valid = valid
        self.errors = errors

    def __repr__(self) -> str:
        if self.valid:
            return "ValidationResult(valid=True)"
        return f"ValidationResult(valid=False, errors={self.errors!r})"
