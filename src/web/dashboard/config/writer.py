"""manage-config-tab dev-checklist #3 — ruamel.yaml round-trip writer。

把 ``config/setting.yaml`` 的少量字段做"保注释 / 保空行 / 保字段顺序"写回，
带：

- ``fcntl.flock`` 文件锁 — 防止同进程并发或多进程同时改
- ``mtime`` 冲突检测 — 上一次 ``read_raw`` 给出 ``expected_mtime``，写回前
  若文件被外部改过，立即报 :class:`ConflictError`（HTTP 409 语义）
- ``ruamel.yaml(typ="rt")`` round-trip — 保留 yaml 注释 + 空行 + key 顺序
- 临时文件 + 原子 ``Path.replace`` — 写盘中途断电只会留下"老文件完整"或
  "新文件完整"，不会出现半截文件
- 写完跑 :func:`config_loader.load_config` 做 pydantic 校验 — 校验失败立刻
  删临时文件，原文件**不动**，把错误翻译成 ``[{"path", "message"}]`` 抛出

参考：``safety._grant_persister`` 已有 ruamel round-trip 模式（追加列表场景，
不带 mtime / 校验 / pydantic 闭环；本模块是"任意字段定点修改 + 校验回滚"的
更通用版本，不复用其代码）。

不依赖（边界）：

- 仅 import ``config_loader.{loader, errors}`` 做 pydantic 校验闭环
- **不** import 任何 ``web.*`` sibling — 由 ``ConfigManager`` 在更上层装配
- **不** 修改 ``yaml_path`` 之外的任何文件
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from config_loader.errors import ConfigLoadError, ConfigValidationError
from config_loader.loader import load_config

# ---------------------------------------------------------------------------
# 公开异常类型
# ---------------------------------------------------------------------------


class ConfigWriterError(Exception):
    """writer 基类异常。所有 writer 异常都派生自此类，便于上层一把 except。"""


class ConflictError(ConfigWriterError):
    """mtime 冲突：文件被外部修改过（HTTP 409 语义）。

    触发场景：

    - 多窗口/多客户端同时编辑同一份 yaml；
    - 用户 ``read_raw`` 后过了一段时间再 ``save_patch``，期间有人手改了文件。

    携带 ``expected_mtime`` / ``actual_mtime`` 便于上层日志定位。
    """

    def __init__(self, expected_mtime: float, actual_mtime: float, path: Path) -> None:
        self.expected_mtime = expected_mtime
        self.actual_mtime = actual_mtime
        self.path = path
        super().__init__(
            f"mtime conflict on {path}: expected={expected_mtime!r}, actual={actual_mtime!r}"
        )


class ValidationFailedError(ConfigWriterError):
    """pydantic 校验失败，含每字段错误清单（HTTP 422 语义）。

    ``errors`` 形态：

    .. code-block:: python

        [
            {"path": "model.temperature", "message": "must be in [0, 2]"},
            {"path": "evolution.memory.enabled", "message": "must be bool"},
        ]
    """

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__(f"validation failed: {len(errors)} errors")


# ---------------------------------------------------------------------------
# 公开数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchItem:
    """一次 patch 单元：把 ``yaml_path`` 中点分路径 ``path`` 的值设为 ``value``。

    ``path`` 形如 ``"model.temperature"`` / ``"evolution.memory.enabled"``——
    仅支持嵌套 dict；list 索引（``"foo.0.bar"``）一期不支持，遇到立即抛
    :class:`ValidationFailedError`。

    ``value`` 必须是 yaml-serializable（标量 / list / dict）；ruamel 会接管
    实际的 yaml 表达层。
    """

    path: str
    value: Any


@dataclass(frozen=True)
class WriteResult:
    """写回成功后的回执。

    ``new_mtime``：刚 rename 后文件的 mtime，供前端更新本地缓存（下次 save
    再传回作为 expected_mtime）。

    ``diff_lines``：新旧 yaml 文本逐行差异行数，供 audit 与"注释 / 空行真的
    保留了吗"测试断言。最小语义 = 改 1 字段 → diff_lines 接近 1。
    """

    new_mtime: float
    diff_lines: int


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def round_trip_update(
    yaml_path: Path,
    patch: Iterable[PatchItem],
    expected_mtime: float,
) -> WriteResult:
    """保注释 + 校验 + 原子写回。

    流程（与 README 技术设计一致）：

    1. 抢文件锁（``fcntl.flock`` LOCK_EX）。
    2. ``os.stat(yaml_path).st_mtime`` 对比 ``expected_mtime``，不一致 →
       :class:`ConflictError`。
    3. ``YAML(typ="rt")`` 加载（产出 ``CommentedMap``，保住注释 / 空行 / 顺序）。
    4. 按点分路径设值；嵌套 dict 缺中间层 / 试图穿过非 dict 节点 → 立刻抛
       :class:`ValidationFailedError`（不允许"魔法"补结构）。
    5. dump 到同目录临时文件 ``.yaml.tmp.<pid>.<nanos>``。
    6. :func:`config_loader.load_config` 跑一次 pydantic 校验；失败 → 删临时
       文件、原文件不动、抛 :class:`ValidationFailedError`。
    7. 校验通过 → ``Path.replace`` 原子覆盖原文件。
    8. 返回 :class:`WriteResult`（含 new_mtime 与 diff_lines）。

    Args:
        yaml_path: 待写回的 yaml 绝对路径。父目录必须已存在；本函数不创建目录。
        patch: 待写入的字段集合。空 iterable → 仍会跑校验链路，但 diff_lines=0。
        expected_mtime: 上一次读到的 mtime。``-1.0`` / ``0.0`` 等"显式表示
            不校验"由调用方自负责任——本函数依然按数值比较。

    Raises:
        FileNotFoundError: ``yaml_path`` 不存在。
        ConflictError: mtime 不匹配（HTTP 409）。
        ValidationFailedError: 路径解析失败 / pydantic 校验失败（HTTP 422）。
        OSError: 文件 IO 失败（权限 / 磁盘满 等）。
    """
    if not yaml_path.exists():
        raise FileNotFoundError(f"round_trip_update: yaml_path not found: {yaml_path}")

    patch_list = list(patch)
    tmp_path: Path | None = None

    # 文件锁通过对原文件加 LOCK_EX 实现：用单独 fd，不污染后续 read/write。
    # 注意：Path.open 不能直接拿到 fd，所以用 os.open。
    lock_fd = os.open(str(yaml_path), os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # 2) mtime check
        actual_mtime = yaml_path.stat().st_mtime
        if not _mtime_equal(actual_mtime, expected_mtime):
            raise ConflictError(
                expected_mtime=expected_mtime,
                actual_mtime=actual_mtime,
                path=yaml_path,
            )

        # 3) ruamel rt 加载
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        with yaml_path.open("r", encoding="utf-8") as f:
            doc = yaml.load(f)
        if doc is None:
            # 空 yaml：用空 dict 兜底（pydantic 会在 7 处揭露 "model 必填" 等错）
            doc = {}

        original_text = yaml_path.read_text(encoding="utf-8")

        # 4) 按 path 设值（嵌套 dict）
        for item in patch_list:
            _set_by_dot_path(doc, item.path, item.value)

        # 5) dump 到临时文件（同目录，避免跨设备 rename 失败 + 暴露原子语义）
        tmp_path = _make_tmp_path(yaml_path)
        new_text = _dump_to_string(yaml, doc)
        tmp_path.write_text(new_text, encoding="utf-8")

        # 6) pydantic 校验
        try:
            load_config(tmp_path, load_env_file=False)
        except ConfigValidationError as exc:
            errors = _translate_validation_errors(exc)
            raise ValidationFailedError(errors) from exc
        except ConfigLoadError as exc:
            # YAML 自身解析失败等：归到 validation 错误反馈给前端
            raise ValidationFailedError([{"path": "<root>", "message": str(exc)}]) from exc

        # 7) 原子 rename（成功后 tmp_path 自动不存在）
        tmp_path.replace(yaml_path)

        # 8) 汇总回执
        new_mtime = yaml_path.stat().st_mtime
        diff_lines = _count_diff_lines(original_text, new_text)
        return WriteResult(new_mtime=new_mtime, diff_lines=diff_lines)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
        # tmp 文件清理：所有 raise 路径都没走到 tmp_path.replace；正常 return
        # 路径 replace 成功后 tmp_path 已经不存在。无论哪种情况，残留就删。
        if tmp_path is not None and tmp_path.exists():
            # 清理失败不应掩盖原异常；写日志的责任在上层
            with contextlib.suppress(OSError):
                tmp_path.unlink()


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _mtime_equal(a: float, b: float) -> bool:
    """mtime 比较：浮点精度容忍 1 微秒。

    Linux ext4 mtime 是 1ns 精度，macOS APFS 也是 1ns，但 Python ``stat``
    给出的 float 在大数值下精度会掉到 1µs；用宽松窗口避免误判冲突。
    """
    return abs(a - b) < 1e-6


def _set_by_dot_path(doc: Any, dot_path: str, value: Any) -> None:
    """按 ``"a.b.c"`` 写入嵌套 dict。

    严格约束：

    - 中间任何一层不是 dict-like → ``ValidationFailedError``
    - 最后一层 key 不存在 → ``ValidationFailedError``（不补结构）

    "不补结构"的设计取舍：本函数服务 manage UI 的"修改已有字段"场景，
    schema 已经枚举了允许的字段路径；如果一个路径写不进 yaml，多半是 schema
    与 yaml 漂移了——这种情况应该立即报错，而不是悄悄给 yaml 加一个
    pydantic 不认识的新字段。
    """
    parts = dot_path.split(".")
    if not parts or any(p == "" for p in parts):
        raise ValidationFailedError([{"path": dot_path, "message": "empty or malformed dot path"}])

    cursor = doc
    for idx, key in enumerate(parts[:-1]):
        if not _is_dict_like(cursor):
            traversed = ".".join(parts[: idx + 1])
            raise ValidationFailedError(
                [{"path": traversed, "message": "path traverses a non-dict node"}]
            )
        if key not in cursor:
            traversed = ".".join(parts[: idx + 1])
            raise ValidationFailedError(
                [{"path": traversed, "message": "intermediate key not present in yaml"}]
            )
        cursor = cursor[key]

    last_key = parts[-1]
    if not _is_dict_like(cursor):
        raise ValidationFailedError([{"path": dot_path, "message": "leaf parent is not a dict"}])
    if last_key not in cursor:
        raise ValidationFailedError([{"path": dot_path, "message": "leaf key not present in yaml"}])
    cursor[last_key] = value


def _is_dict_like(node: Any) -> bool:
    """ruamel ``CommentedMap`` 与普通 ``dict`` 都满足 ``__getitem__`` /
    ``__setitem__`` / ``__contains__``；用 isinstance(dict) 太严，用
    hasattr 元组判断更稳。"""
    return (
        hasattr(node, "__getitem__")
        and hasattr(node, "__setitem__")
        and hasattr(node, "__contains__")
        and not isinstance(node, (str, bytes, list, tuple))
    )


def _make_tmp_path(yaml_path: Path) -> Path:
    """同目录临时文件名 ``<name>.tmp.<pid>.<nanos>``。

    放同目录保证 ``Path.replace`` 是原子 rename（跨文件系统 rename 不原子）。
    pid + nanos 让并发调用的临时文件不撞车。
    """
    nanos = time.time_ns()
    pid = os.getpid()
    return yaml_path.with_name(f"{yaml_path.name}.tmp.{pid}.{nanos}")


def _dump_to_string(yaml: YAML, doc: Any) -> str:
    """ruamel YAML.dump 只接受 stream；用 StringIO 转字符串。"""
    buf = StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


def _translate_validation_errors(exc: ConfigValidationError) -> list[dict[str, str]]:
    """把 pydantic 的 ``ValidationError.errors()`` 翻译成对外友好格式。

    ``ConfigValidationError.details`` 在 ``loader.py`` 里塞了
    ``{"errors": exc.errors(), "path": ...}``——这是 pydantic v2 errors() 列表。
    单条形如 ``{"loc": ("model", "temperature"), "msg": "...", "type": "..."}``。
    """
    details: Any = getattr(exc, "details", None) or {}
    raw_errors: Any = details.get("errors") if isinstance(details, dict) else None
    if not isinstance(raw_errors, list):
        # 没法解析 → 全文给一条
        return [{"path": "<root>", "message": str(exc)}]

    out: list[dict[str, str]] = []
    for err in raw_errors:
        if not isinstance(err, dict):
            continue
        loc = err.get("loc") or ()
        path = ".".join(str(p) for p in loc) if isinstance(loc, (list, tuple)) else str(loc)
        msg = str(err.get("msg") or err.get("type") or "validation error")
        out.append({"path": path, "message": msg})

    if not out:
        return [{"path": "<root>", "message": str(exc)}]
    return out


def _count_diff_lines(old: str, new: str) -> int:
    """新旧文本逐行差异数（symmetric，行级粒度）。

    实现：``difflib.ndiff`` 计 ``-`` / ``+`` 行，二者之和即"实际改动的行数"。
    用于 audit 与测试断言"改 1 字段 diff 行数小"。这里不引入 ``difflib`` 额外
    依赖性能开销也可以接受——本函数只在 save 成功路径调用一次。
    """
    import difflib

    diff = difflib.ndiff(old.splitlines(), new.splitlines())
    count = 0
    for line in diff:
        if line.startswith("- ") or line.startswith("+ "):
            count += 1
    return count


__all__ = [
    "ConfigWriterError",
    "ConflictError",
    "PatchItem",
    "ValidationFailedError",
    "WriteResult",
    "round_trip_update",
]
