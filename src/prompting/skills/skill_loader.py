"""Skill 加载器 — 扫描双源目录、解析 frontmatter、渲染 listing 文本。

v0.1.6 范围（详见 ``docs/skill-system-v0.1.6/``）：

- **双源加载**：``<home>/skills/<name>/SKILL.md`` 与
  ``<workspace>/.kongming/skills/<name>/SKILL.md``。
- **同名覆盖**：workspace 覆盖 home，emit ``skill.shadowed`` 事件。
- **不读 body**：仅记录 ``body_path``，正文加载留给 SkillTool（模块 B）。
- **frontmatter 5 字段**进 :class:`SkillSpec`：``name`` / ``description`` /
  ``when_to_use`` / ``allowed_tools`` / ``paths``。``allowed_tools`` 与
  ``paths`` v0.1.6 仅解析存好，不被运行时消费。
- **高级字段静默忽略**（``model`` / ``effort`` / ``hooks`` 等）——前向兼容
  Claude Code SKILL.md，但不实现其语义。

设计与依赖约束：

- 与 :mod:`prompting.instructions.prompts_loader` 同层：纯装配期 loader，所有 I/O 走
  :func:`asyncio.to_thread`，对外暴露异步入口。
- :class:`SkillSpec` 用 ``dataclass(frozen=True)`` 保证多线程共享安全。
- 错误分级：缺目录静默 / 权限错误冒泡 / 单 skill 解析失败跳过单条 + emit
  ``skill.parse_failed`` 事件。
- 不 import ``tools`` / ``executors`` / ``safety`` / ``host`` / ``cli``——
  ``prompting`` 模块边界（CLAUDE.md 约束）。
- 装配期事件 ``run_id`` 用固定占位 ``"skill_loader"``，与 turn 上的 run_id
  命名空间区分。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from core.contracts import Event, EventSink

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Frontmatter 头匹配：必须以 ``---\n`` 起始，至下一个 ``---`` 行结束。
# ``re.DOTALL`` 让 ``.`` 匹配换行，``+?`` 非贪婪匹配避免吞掉后续内容。
_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---\n?", re.DOTALL)

# listing 中 ``description[+ - + when_to_use]`` 整体的最大字符数。
# 超出截断为 ``desc[:249] + "…"``，含省略号后总长度恰好 250。
_MAX_LISTING_DESC_CHARS = 250

# 装配期事件占位 ``run_id``：loader 跑在装配阶段，没有 turn 上下文。
_LOADER_RUN_ID = "skill_loader"

# 双源目录子路径与 skill 文件名。
_SKILLS_SUBDIR = "skills"
_WORKSPACE_SUBDIR = ".kongming/skills"
_SKILL_FILENAME = "SKILL.md"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillSpec:
    """单个 skill 的解析结果（不可变）。

    ``body_path`` 指向 ``SKILL.md`` 文件，加载阶段不读，调用阶段才读。
    ``allowed_tools`` 与 ``paths`` 在 v0.1.6 仅解析存好，不被运行时消费——
    目的是让 SKILL.md 编写者已按目标语义书写，v0.2 接入时不破坏向后兼容。
    """

    name: str
    description: str
    when_to_use: str | None
    allowed_tools: list[str] | None
    paths: list[str] | None
    body_path: Path
    source: Literal["home", "workspace"]


@dataclass(frozen=True)
class _LoaderEvent:
    """同步扫描层暂存的事件。

    ``_scan_source_sync`` 在 ``asyncio.to_thread`` 边界外执行，不能直接持有
    ``EventSink`` 引用（async 协议）。所以扫描期间用 ``_LoaderEvent`` 暂存
    ``(kind, payload)``，回到 async 入口后再统一通过 :func:`_fanout_events`
    包成 :class:`Event` 并 fan-out。
    """

    kind: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# 内部工具：frontmatter 解析与字段构建
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    """剥离 ``---\\n...\\n---`` 头并 :func:`yaml.safe_load`。

    Args:
        text: SKILL.md 全文。

    Returns:
        解析得到的 dict；以下情形返回 ``None``：

        - 文本不以 ``---\\n`` frontmatter 头起始；
        - YAML 解析失败（:class:`yaml.YAMLError`）；
        - YAML 顶层不是 dict（如 list / str / int）。

    严格使用 :func:`yaml.safe_load`——禁止 :func:`yaml.load`，避免恶意
    frontmatter 触发任意 Python 对象加载（YAML 注入风险）。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def _build_spec_from_dict(
    meta: dict[str, Any],
    body_path: Path,
    source: Literal["home", "workspace"],
) -> SkillSpec | None:
    """从 frontmatter dict 构造 :class:`SkillSpec`。

    必填字段（缺失任一即返回 ``None``）：

    - ``name``：非空 string；
    - ``description``：非空 string。

    可选字段同时接受 dash（spec 标准）与 underscore（python 风格）写法，
    降低用户书写心智负担：

    - ``when-to-use`` / ``when_to_use``
    - ``allowed-tools`` / ``allowed_tools``
    - ``paths``

    高级字段（``model`` / ``effort`` / ``hooks`` 等）静默忽略，不进 SkillSpec
    也不触发错误，前向兼容 Claude Code SKILL.md。
    """
    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(description, str) or not description.strip():
        return None

    when_to_use = meta.get("when-to-use")
    if when_to_use is None:
        when_to_use = meta.get("when_to_use")
    allowed = meta.get("allowed-tools")
    if allowed is None:
        allowed = meta.get("allowed_tools")
    paths = meta.get("paths")

    return SkillSpec(
        name=name.strip(),
        description=description.strip(),
        when_to_use=when_to_use.strip() if isinstance(when_to_use, str) else None,
        allowed_tools=[str(t) for t in allowed] if isinstance(allowed, list) else None,
        paths=[str(p) for p in paths] if isinstance(paths, list) else None,
        body_path=body_path,
        source=source,
    )


def _is_safe_path(skill_dir: Path, body_path: Path) -> bool:
    """``body_path`` realpath 后必须落在 ``skill_dir`` realpath 内。

    防御场景：用户在 skill 目录里放 ``SKILL.md`` 是符号链接，链到
    ``/etc/passwd`` 或仓库外文件——loader 不应跟随此类逃逸。

    Args:
        skill_dir: 单个 skill 的目录（如 ``<home>/skills/commit``）。
        body_path: ``SKILL.md`` 文件路径（可能是符号链接）。

    Returns:
        body_path 真实路径在 skill_dir 真实路径内时返回 ``True``；
        路径不存在 / 解析失败 / 真实路径在外面均返回 ``False``。

    实现走 :meth:`Path.resolve` ``strict=True``——不存在文件直接报
    :class:`OSError`，避免对幽灵路径放行。
    """
    try:
        real_body = body_path.resolve(strict=True)
        real_dir = skill_dir.resolve(strict=True)
    except OSError:
        return False
    try:
        real_body.relative_to(real_dir)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# 物理扫描（同步，asyncio.to_thread 包装）
# ---------------------------------------------------------------------------


def _scan_source_sync(
    root: Path,
    source: Literal["home", "workspace"],
) -> tuple[list[SkillSpec], list[_LoaderEvent]]:
    """扫描单源目录下所有 ``<skill>/SKILL.md``。

    Args:
        root: skills 根目录（``<home>/skills`` 或 ``<workspace>/.kongming/skills``）。
        source: 来源标识，写入 :class:`SkillSpec.source` 与事件 payload。

    Returns:
        ``(specs, events)`` 二元组。``events`` 同步暂存为
        :class:`_LoaderEvent`，由调用方在 async 边界统一 fan-out。

    错误处理：

    - 根目录不存在 / 不是目录 → 静默返回空（用户没装 skill 是常态）；
    - :class:`PermissionError` 让其冒泡（与 :mod:`prompting.instructions.prompts_loader`
      一致，明确失败优于静默 degrade）；
    - 单个 SKILL.md 读取或解析失败 → 跳过该条 + emit ``skill.parse_failed``，
      不影响其他 skill 加载；
    - 符号链接逃逸 → emit ``skill.skipped`` + 不进 SkillSpec。
    """
    specs: list[SkillSpec] = []
    events: list[_LoaderEvent] = []
    if not root.exists():
        return specs, events
    if not root.is_dir():
        return specs, events

    # PermissionError 让其冒泡——与 prompts_loader 一致。
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        body_path = entry / _SKILL_FILENAME
        if not body_path.exists():
            continue
        if not _is_safe_path(entry, body_path):
            events.append(
                _LoaderEvent(
                    kind="skill.skipped",
                    payload={
                        "path": str(body_path),
                        "reason": "symlink_escapes_skill_dir",
                    },
                )
            )
            continue
        try:
            text = body_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            events.append(
                _LoaderEvent(
                    kind="skill.parse_failed",
                    payload={
                        "path": str(body_path),
                        "error_kind": type(e).__name__,
                        "message": str(e),
                    },
                )
            )
            continue
        meta = _parse_frontmatter(text)
        if meta is None:
            events.append(
                _LoaderEvent(
                    kind="skill.parse_failed",
                    payload={
                        "path": str(body_path),
                        "error_kind": "frontmatter_invalid",
                        "message": "missing or malformed frontmatter",
                    },
                )
            )
            continue
        spec = _build_spec_from_dict(meta, body_path, source)
        if spec is None:
            events.append(
                _LoaderEvent(
                    kind="skill.parse_failed",
                    payload={
                        "path": str(body_path),
                        "error_kind": "missing_required_field",
                        "message": "name or description missing",
                    },
                )
            )
            continue
        specs.append(spec)
        events.append(
            _LoaderEvent(
                kind="skill.discovered",
                payload={
                    "name": spec.name,
                    "source": source,
                    "body_path": str(body_path),
                },
            )
        )
    return specs, events


# ---------------------------------------------------------------------------
# 双源合并
# ---------------------------------------------------------------------------


def _merge_sources(
    home_specs: Sequence[SkillSpec],
    workspace_specs: Sequence[SkillSpec],
) -> tuple[list[SkillSpec], list[_LoaderEvent]]:
    """workspace 覆盖 home，按 ``name`` 去重。

    覆盖时 emit ``skill.shadowed`` 事件，让 trace 可查——避免用户察觉不到
    workspace 的同名 skill 默默盖掉 home 版本。

    Args:
        home_specs: home 源已解析的 SkillSpec 列表。
        workspace_specs: workspace 源已解析的 SkillSpec 列表。

    Returns:
        ``(merged_specs, events)``。``merged_specs`` 顺序：先 home 不被覆盖
        的，再插入 workspace；同名条目 workspace 替换 home 但保留原插入顺序。
    """
    merged: dict[str, SkillSpec] = {s.name: s for s in home_specs}
    events: list[_LoaderEvent] = []
    for ws in workspace_specs:
        if ws.name in merged:
            events.append(
                _LoaderEvent(
                    kind="skill.shadowed",
                    payload={"name": ws.name, "shadowed_source": "home"},
                )
            )
        merged[ws.name] = ws
    return list(merged.values()), events


# ---------------------------------------------------------------------------
# listing 渲染
# ---------------------------------------------------------------------------


def format_skill_listing(specs: Sequence[SkillSpec]) -> str:
    """渲染 skills listing 文本（注入到 system prompt 的纯文本块）。

    每行格式::

        - <name>: <description>[ - <when_to_use>]

    截断规则（与 Claude Code ``prompt.ts:43-50, 65`` 对齐）：

    - 当 ``description`` + ``" - "`` + ``when_to_use`` 拼起来字符数 > 250 时，
      取 ``desc[:249] + "…"``，含省略号后整体恰好 250 字。
    - ``name`` 不参与截断——模型必须看到完整 name 才能 ``tool_call``，
      name 长度由用户自律（通常 < 30 字）。
    - 中文 / emoji 等多 codepoint 字符按 Python ``str`` 切片处理，不会破坏
      codepoint 边界（``str`` 是 codepoint 序列）。

    Args:
        specs: 已解析合并的 SkillSpec 列表。

    Returns:
        多行字符串；空 ``specs`` 返回 ``""``（调用方决定是否注入 origin 头）。
    """
    if not specs:
        return ""
    lines: list[str] = []
    for spec in specs:
        desc = spec.description
        if spec.when_to_use:
            desc = f"{desc} - {spec.when_to_use}"
        if len(desc) > _MAX_LISTING_DESC_CHARS:
            desc = desc[: _MAX_LISTING_DESC_CHARS - 1] + "…"
        lines.append(f"- {spec.name}: {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 事件 fanout
# ---------------------------------------------------------------------------


async def _fanout_events(
    events: Sequence[_LoaderEvent],
    sinks: Sequence[EventSink],
) -> None:
    """把暂存的 :class:`_LoaderEvent` 转为 :class:`Event` 后向所有 sink fan-out。

    每个 sink 在独立 try/except 里 emit——sink 抛异常既不污染主链路，也不
    打断对其他 sink 的 fan-out。与 :mod:`memory.safety_write` 的事件
    fan-out 模式保持一致。
    """
    if not sinks or not events:
        return
    for ev in events:
        event_obj = Event(
            kind=ev.kind,
            run_id=_LOADER_RUN_ID,
            payload=dict(ev.payload),
        )
        for sink in sinks:
            try:
                await sink.emit(event_obj)
            except Exception:
                # sink 异常不污染主链路，也不打断对其他 sink 的 fan-out
                continue


# ---------------------------------------------------------------------------
# 公共 async 入口
# ---------------------------------------------------------------------------


async def load_skill_specs(
    home: Path,
    workspace: Path | None = None,
    *,
    event_sinks: Sequence[EventSink] = (),
) -> list[SkillSpec]:
    """扫描双源目录，返回合并后的 :class:`SkillSpec` 列表。

    Args:
        home: ``.kongming/`` 根目录的绝对路径（通常由
            :func:`config_loader.get_kongming_home` 产出）。loader 在其下
            扫描 ``skills/`` 子目录。
        workspace: 工作空间根（通常 ``cwd``）。loader 在其下扫描
            ``.kongming/skills/`` 子目录。``None`` 表示跳过 workspace 源。
        event_sinks: 装配期事件 fan-out 目标列表。空元组 = 不 emit。

    Returns:
        合并去重后的 SkillSpec 列表。workspace 同名 skill 覆盖 home 版本，
        被覆盖会 emit ``skill.shadowed`` 事件。

    Raises:
        PermissionError / OSError: 目录可访问性失败——视为用户环境配置错误，
            不静默 degrade，让装配阶段明确失败便于排查。

    事件清单（沿用 :class:`EventSink`）：

    - ``skill.discovered``：每成功解析一个 spec；
    - ``skill.shadowed``：workspace 覆盖 home 同名 skill；
    - ``skill.parse_failed``：YAML 解析失败 / 必填字段缺失 / 文件读失败；
    - ``skill.skipped``：符号链接逃逸 skill 目录。
    """
    home_root = home / _SKILLS_SUBDIR
    workspace_root = (workspace / _WORKSPACE_SUBDIR) if workspace else None

    home_specs, home_events = await asyncio.to_thread(_scan_source_sync, home_root, "home")
    if workspace_root is not None:
        ws_specs, ws_events = await asyncio.to_thread(
            _scan_source_sync, workspace_root, "workspace"
        )
    else:
        ws_specs, ws_events = [], []

    merged, merge_events = _merge_sources(home_specs, ws_specs)
    all_events = [*home_events, *ws_events, *merge_events]
    await _fanout_events(all_events, event_sinks)
    return merged


__all__ = ["SkillSpec", "format_skill_listing", "load_skill_specs"]
