"""资产模型 + 文件 IO 抽象层。

- :class:`AttachmentAsset`：资产主记录数据结构。
- :class:`AssetStorage`：文件 IO 抽象层（写/读 bytes + 元数据 JSON）。

抽象动机：未来切对象存储 / Tauri 沙盒文件系统时换实现即可，
调用方（AssetRegistry / UploadController）不感知物理路径。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.contracts import AttachmentKind, AttachmentStatus
from infrastructure.config import get_kongming_home

__all__ = [
    "AssetStorage",
    "AttachmentAsset",
    "AttachmentAssetRef",
    "AttachmentKind",
    "AttachmentStatus",
]


@dataclass(frozen=True)
class AttachmentAsset:
    """资产主记录。

    与 ``web.protocol.ws_frames.UserInputAttachment`` 形状对齐，但多了
    服务端字段（thread_id / sha256 / storage_path / created_at）。
    """

    asset_id: str
    thread_id: str
    kind: AttachmentKind
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    sha256: str = ""
    storage_path: str = ""
    preview_url: str = ""
    status: AttachmentStatus = "ready"
    created_at: float = 0.0


@dataclass(frozen=True, order=True)
class AttachmentAssetRef:
    """历史消息对一个 ready 附件的稳定引用。"""

    kind: AttachmentKind
    asset_id: str


# kind → 目录名映射。新增 video / file 时在这里加一条即可。
_KIND_DIR: dict[AttachmentKind, str] = {
    "image": "images",
    "video": "videos",
    "file": "files",
}
_ASSET_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class AssetStorage:
    """资产文件 IO 抽象层。

    职责：把 bytes 写到 / 读取自
    ``<base_dir>/<kind>s/<thread_id>/<asset_id>.<ext>``，
    同目录写 ``<asset_id>.json`` 元数据文件。

    抽象动机：未来切对象存储 / Tauri 沙盒文件系统时换实现即可，
    调用方（AssetRegistry / UploadController）不感知物理路径。
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        """初始化存储根目录。

        Args:
            base_dir: 资产根目录。默认 ``get_kongming_home() / "web" / "uploads"``。
        """

        if base_dir is None:
            base_dir = get_kongming_home() / "web" / "uploads"
        self._base_dir = base_dir

    @property
    def base_dir(self) -> Path:
        """资产根目录绝对路径。"""

        return self._base_dir

    # ------------------------------------------------------------------ 路径
    def _kind_dir(self, kind: AttachmentKind) -> str:
        """kind → 子目录名映射。扩展 video / file 时改这里。"""

        try:
            return _KIND_DIR[kind]
        except KeyError as exc:  # pragma: no cover - 防御 Literal 之外的值
            raise ValueError(f"unsupported attachment kind: {kind!r}") from exc

    def asset_path(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
        ext: str,
    ) -> Path:
        """计算资产文件路径（不读文件，纯路径计算）。

        Args:
            asset_id: 资产 ID。
            thread_id: 所属 thread ID。
            kind: 资产类型（image / video / file）。
            ext: 文件扩展名（含点号，例如 ``".png"``）。
        """

        return self._base_dir / self._kind_dir(kind) / thread_id / f"{asset_id}{ext}"

    def _metadata_path(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
    ) -> Path:
        """元数据 JSON 文件路径。"""

        return self._base_dir / self._kind_dir(kind) / thread_id / f"{asset_id}.json"

    def _thread_dir(self, *, thread_id: str, kind: AttachmentKind) -> Path:
        """某 thread 在某 kind 下的目录。"""

        return self._base_dir / self._kind_dir(kind) / thread_id

    # ------------------------------------------------------------------ 写
    def write_asset(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
        ext: str,
        payload: bytes,
        metadata: dict[str, Any],
    ) -> Path:
        """写入资产 bytes + 同目录 ``<asset_id>.json`` 元数据。

        采用原子写：先写 ``<asset_id>.<ext>.tmp`` 再 ``os.rename``，
        避免读取方看到半写状态的文件。

        Args:
            asset_id: 资产 ID。
            thread_id: 所属 thread ID。
            kind: 资产类型。
            ext: 文件扩展名（含点号）。
            payload: 资产二进制内容。
            metadata: 元数据 dict（通常来自 ``AttachmentAsset.__dict__``）。

        Returns:
            资产文件的绝对路径。
        """

        target = self.asset_path(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
            ext=ext,
        )
        meta_target = self._metadata_path(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        # 原子写资产 bytes
        tmp_payload = target.with_name(target.name + ".tmp")
        tmp_payload.write_bytes(payload)
        os.replace(tmp_payload, target)

        # 原子写元数据 JSON
        meta_text = json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_meta = meta_target.with_name(meta_target.name + ".tmp")
        tmp_meta.write_text(meta_text, encoding="utf-8")
        os.replace(tmp_meta, meta_target)

        return target.resolve()

    # ------------------------------------------------------------------ 读
    def read_asset_bytes(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
        ext: str,
    ) -> bytes:
        """读取资产 bytes（给 §4 multimodal-assembly 用）。

        Raises:
            FileNotFoundError: 资产不存在。
        """

        path = self.asset_path(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
            ext=ext,
        )
        return path.read_bytes()

    def read_asset_metadata(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
    ) -> dict[str, Any]:
        """读取 ``<asset_id>.json`` 元数据。

        Raises:
            FileNotFoundError: 元数据文件不存在。
            json.JSONDecodeError: 元数据格式损坏。
        """

        meta_path = self._metadata_path(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
        )
        raw = meta_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
        return data

    def asset_exists(
        self,
        *,
        asset_id: str,
        thread_id: str,
        kind: AttachmentKind,
        ext: str,
    ) -> bool:
        """检查资产文件是否存在。"""

        return self.asset_path(
            asset_id=asset_id,
            thread_id=thread_id,
            kind=kind,
            ext=ext,
        ).is_file()

    # ------------------------------------------------------------------ 删
    def delete_thread_assets(self, *, thread_id: str) -> int:
        """删除该 thread 名下所有 kind 子目录的资产。

        Args:
            thread_id: 要清理的 thread ID。

        Returns:
            实际被删除的文件数量（含 bytes 文件 + ``.json`` 元数据，
            不含目录本身）。
        """

        removed = 0
        for kind in _KIND_DIR:
            dir_path = self._thread_dir(thread_id=thread_id, kind=kind)
            if not dir_path.is_dir():
                continue
            for child in dir_path.rglob("*"):
                if child.is_file():
                    removed += 1
            shutil.rmtree(dir_path)
        return removed

    def copy_thread_assets(
        self,
        *,
        source_thread_id: str,
        target_thread_id: str,
        references: tuple[AttachmentAssetRef, ...],
    ) -> int:
        """复制历史引用的 ready 附件闭包，并把元数据归属改为目标 thread。

        资产 ID 保持稳定，使历史消息里的 ``preview_url`` 和 ``asset_id`` 无需
        重写；目标 thread 获得独立文件副本，源 thread 删除后仍可继续组装多模态
        LLM 输入。每个引用都必须同时具备一份 JSON metadata 和一份 payload；
        缺失、重复 payload 或元数据身份不一致会中止整个 fork。

        Returns:
            复制的文件数，包含 payload 与 JSON 元数据文件。
        """
        copied = 0
        for reference in sorted(set(references)):
            if _ASSET_ID_PATTERN.fullmatch(reference.asset_id) is None:
                raise ValueError(f"invalid attachment asset_id: {reference.asset_id!r}")

            source_dir = self._thread_dir(
                thread_id=source_thread_id,
                kind=reference.kind,
            )
            metadata_source = self._metadata_path(
                asset_id=reference.asset_id,
                thread_id=source_thread_id,
                kind=reference.kind,
            )
            if not metadata_source.is_file():
                raise FileNotFoundError(f"asset metadata missing: {reference.asset_id}")
            payload_sources = [
                candidate
                for candidate in source_dir.iterdir()
                if candidate.is_file()
                and candidate.name.startswith(f"{reference.asset_id}.")
                and candidate.name != f"{reference.asset_id}.json"
                and not candidate.name.endswith(".tmp")
            ]
            if not payload_sources:
                raise FileNotFoundError(f"asset payload missing: {reference.asset_id}")
            if len(payload_sources) > 1:
                raise ValueError(f"asset payload is ambiguous: {reference.asset_id}")

            metadata = json.loads(metadata_source.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError(f"asset metadata must be an object: {reference.asset_id}")
            if metadata.get("asset_id") != reference.asset_id:
                raise ValueError(f"asset metadata id mismatch: {reference.asset_id}")
            if metadata.get("thread_id") != source_thread_id:
                raise ValueError(f"asset metadata thread mismatch: {reference.asset_id}")
            if metadata.get("kind") != reference.kind:
                raise ValueError(f"asset metadata kind mismatch: {reference.asset_id}")

            payload_source = payload_sources[0]
            target_dir = self._thread_dir(
                thread_id=target_thread_id,
                kind=reference.kind,
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            payload_target = target_dir / payload_source.name
            payload_tmp = payload_target.with_name(payload_target.name + ".tmp")
            shutil.copy2(payload_source, payload_tmp)
            os.replace(payload_tmp, payload_target)
            copied += 1

            metadata["thread_id"] = target_thread_id
            metadata["storage_path"] = (
                f"{self._kind_dir(reference.kind)}/{target_thread_id}/{payload_source.name}"
            )
            metadata_target = self._metadata_path(
                asset_id=reference.asset_id,
                thread_id=target_thread_id,
                kind=reference.kind,
            )
            metadata_tmp = metadata_target.with_name(metadata_target.name + ".tmp")
            metadata_tmp.write_text(
                json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(metadata_tmp, metadata_target)
            copied += 1
        return copied
