"""First-class Skill tool（skill-system-v0.1.6 模块 B / 模块 3）。

职责：模型 ``tool_call("skill", {"skill": <name>, "args"?: <str>})`` 命中本类，
按 name 查 :class:`SkillSpec`，:func:`asyncio.to_thread` 读 ``body_path``，
拒绝 ``!command`` 替换，做单次 ``$ARGUMENTS`` / ``${KONGMING_SKILL_DIR}`` /
``${KONGMING_SESSION_ID}`` 替换，返回 :class:`ToolResult`。

设计要点：

- **不继承** :class:`tools.runtime.base.BaseBuiltinTool`：基类的统一异常包装会把读盘
  失败 / ``!command`` 拒绝吞成 ``ok=False``，runner 之上再无法区分失败原因。
  本类在 :meth:`execute` 内自管 try/except，同时配套 emit ``skill.failed``。
- **三事件 emit**：``skill.invoked`` 在参数校验通过后立即 emit；
  ``skill.completed`` 在替换成功后 emit；``skill.failed`` 覆盖 read /
  decode / ``!command`` 拒绝 / 其他异常的所有失败路径，含 ``error_kind``
  与 ``elapsed_ms``。
- **变量替换不递归**：单次扫描三 ``str.replace``，``$ARGUMENTS`` 内容含
  ``${KONGMING_SKILL_DIR}`` 不会二次展开，避免参数注入。
- **``!command`` 永久禁用**：v0.1.6 不实现 shell 命令替换（与 Claude Code
  设计差异），仅做字面 token 检测，命中即 :class:`SkillSecurityError`。
- **导入边界**：CLAUDE.md 约束 5/6 + import-linter Contract 3 / 4 —— 本模块
  不允许 import ``safety`` / ``executors`` / ``host`` / ``cli`` / ``prompting``。
  ``prompting.skills.skill_loader.SkillSpec`` 与 ``tools`` 同层，不能直接消费；
  用本模块内的 :class:`_SkillSpecLike` Protocol 做结构子类型，由装配层（
  ``skill-assemble-v0.1.6`` 模块）传真 SkillSpec 进来即满足 duck typing。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from core.contracts import Event, EventSink, PreparedToolCall, ToolContext, ToolResult
from core.errors import AgentError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 永久禁用的替换 token；v0.1.6 不实现 shell 命令替换。
# 字面全字匹配，不用正则，避免 markdown body 中无关 ``!`` 标点被误伤。
_FORBIDDEN_TOKENS: Final[tuple[str, ...]] = ("!command",)

# 单次扫描替换三 token 的正则；``re.sub`` 单遍即完成，注入值含 token 字面
# 也不会二次展开（参数注入防御）。``\$`` 锚定字面 ``$``；``$ARGUMENTS`` 用
# ``\b`` 词边界避免误匹配如 ``$ARGUMENTSX`` 这类粘连串。
_SUBSTITUTE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$ARGUMENTS\b|\$\{KONGMING_SKILL_DIR\}|\$\{KONGMING_SESSION_ID\}"
)

# v0.1.6 SKILL_TOOL_SCHEMA：与 ``ReadFileTool`` / ``ShellTool`` 同形态。
SKILL_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "Skill name as listed under '# skills' in the system prompt.",
        },
        "args": {
            "type": "string",
            "description": (
                "Optional arguments substituted into $ARGUMENTS within the skill body. "
                "Defaults to empty string when omitted."
            ),
        },
    },
    "required": ["skill"],
}


# ---------------------------------------------------------------------------
# SkillSpec 结构子类型（duck-type，避免 tools → prompting 跨层 import）
# ---------------------------------------------------------------------------


@runtime_checkable
class _SkillSpecLike(Protocol):
    """:class:`prompting.skills.skill_loader.SkillSpec` 的结构子类型。

    本类只读 ``name`` / ``source`` / ``body_path`` 三个字段；保持 Protocol 极简
    使装配层传任意"长得像"的对象都能跑通。``prompting.skills.skill_loader.SkillSpec``
    天然满足此 Protocol——frozen dataclass + 上述三字段。

    放在本文件而非 ``core.contracts``：v0.1.6 阶段 SkillSpec 形态只在
    skill 系统内消费，不需要全局 contracts 真源；Protocol 局部化降低
    耦合面。如果 v0.2+ 多模块共享 SkillSpec，再上提到 ``core.contracts``。
    """

    name: str
    source: str
    body_path: Path


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SkillSecurityError(AgentError):
    """skill body 含禁止替换 token（如 ``!command``）。

    专用异常便于上层定向识别；不冒泡到 runner——
    :meth:`SkillTool.execute` 内部 try/except 转 ``ToolResult(ok=False, ...)``，
    与现有 builtin tools 失败模式一致，不打断 turn。
    """


# ---------------------------------------------------------------------------
# 纯函数：变量替换 + 禁止 token 检测
# ---------------------------------------------------------------------------


def substitute_vars(
    body: str,
    *,
    args: str,
    skill_dir: str,
    session_id: str,
) -> str:
    """对 skill body 做单次三变量替换。

    Args:
        body: 原始 SKILL.md 正文（已剥 frontmatter 由 SkillSpec 持有 body_path
            指向同一文件——v0.1.6 阶段简化为整文件读取，frontmatter 一并替换）。
        args: 模型 tool_call 入参 ``args``，注入到 ``$ARGUMENTS``。
        skill_dir: skill 所在目录绝对路径，注入到 ``${KONGMING_SKILL_DIR}``。
        session_id: 当前 session id，注入到 ``${KONGMING_SESSION_ID}``。

    Returns:
        替换后的字符串。**不递归**：``$ARGUMENTS`` 内容里再写
        ``${KONGMING_SKILL_DIR}`` 不会二次展开，避免参数注入。

    实现说明：
    单次正则扫描，由 ``re.sub`` 一次遍历完成；注入值若再含 ``$ARGUMENTS``
    或 ``${KONGMING_*}`` 字面也不会二次展开（参数注入防御）。
    """
    mapping = {
        "$ARGUMENTS": args,
        "${KONGMING_SKILL_DIR}": skill_dir,
        "${KONGMING_SESSION_ID}": session_id,
    }
    return _SUBSTITUTE_PATTERN.sub(lambda m: mapping[m.group(0)], body)


def assert_no_command_substitution(body: str) -> None:
    """字面 token 全字检测；命中任一 ``_FORBIDDEN_TOKENS`` 即抛
    :class:`SkillSecurityError`。

    v0.1.6 永久禁用 ``!command`` shell 替换语义；用户若在 skill body 中
    自然包含 ``!`` 标点（如惊叹句、shebang 等）但不含 ``!command`` 字面，
    本检测不会误伤。
    """
    for token in _FORBIDDEN_TOKENS:
        if token in body:
            raise SkillSecurityError(
                f"!command substitution is disabled in v0.1.6 "
                f"(skill body contains forbidden token {token!r})",
                details={"forbidden_token": token},
            )


# ---------------------------------------------------------------------------
# SkillTool
# ---------------------------------------------------------------------------


class SkillTool:
    """First-class skill tool（实现 :class:`core.contracts.Tool` Protocol）。

    Args:
        specs: ``name -> SkillSpec`` 映射。装配层（``skill-assemble-v0.1.6``）
            把 :func:`prompting.skills.skill_loader.load_skill_specs` 结果转 dict 后传入。
            本类不持有 loader 引用，也不在运行时再次扫描磁盘——SkillSpec 一经
            装配即冻结，热重载留给 v0.2+。
        event_sinks: 运行时事件 sink 列表。空元组 = 不 emit。
            sink 抛异常不污染主链路（per-sink try/except）。

    不继承 :class:`tools.runtime.base.BaseBuiltinTool` 的原因见模块 docstring。
    """

    name: Final[str] = "skill"
    description: Final[str] = (
        "Execute a skill by name. The skill body is read on demand and returned "
        "as the tool result. Available skills are listed in the system prompt "
        "under the '# skills' section."
    )
    input_schema: Final[dict[str, Any]] = SKILL_TOOL_SCHEMA

    def __init__(
        self,
        *,
        specs: Mapping[str, _SkillSpecLike],
        event_sinks: Sequence[EventSink] = (),
    ) -> None:
        # 拷贝防外部突变；SkillSpec 本身 frozen，dict 浅拷即可。
        self._specs: dict[str, _SkillSpecLike] = dict(specs)
        self._sinks: tuple[EventSink, ...] = tuple(event_sinks)

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 skill/args 并冻结标准化参数。"""
        del context
        if not isinstance(arguments, dict):
            raise TypeError(f"tool args must be dict, got {type(arguments).__name__}")
        skill_name_raw = arguments.get("skill")
        if not isinstance(skill_name_raw, str) or not skill_name_raw.strip():
            raise ValueError("'skill' parameter is required and must be a non-empty string")
        args_value = arguments.get("args", "")
        if args_value is None:
            args_value = ""
        if not isinstance(args_value, str):
            raise TypeError("'args' must be a string when provided")
        return PreparedToolCall(
            arguments={
                "skill": skill_name_raw.strip(),
                "args": args_value,
            }
        )

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """7-step skill execution flow（与 plan.md 模块 3 锁定的契约一致）。

        步骤：

        1. 参数校验：``skill`` 必填非空 string；``args`` 可选 string，默认 ``""``。
        2. 查 ``self._specs[name]``——unknown skill 直接返回 error，**不 emit**
           ``skill.invoked`` / ``skill.failed``（避免污染监控）。
        3. emit ``skill.invoked``。
        4. ``asyncio.to_thread`` 读 body；OSError / UnicodeDecodeError → emit
           ``skill.failed`` + 返回 error。
        5. ``assert_no_command_substitution``；命中 → emit ``skill.failed`` +
           返回 error。
        6. ``substitute_vars`` 单次替换。
        7. emit ``skill.completed`` + 返回 ``ToolResult(ok=True, content=substituted)``。
        """
        # Step 1: 参数已由 prepare 校验和标准化。
        skill_name = str(prepared.arguments["skill"])
        args_value = str(prepared.arguments["args"])

        # Step 2: 查 spec。unknown skill 不 emit invoked / failed。
        spec = self._specs.get(skill_name)
        if spec is None:
            return ToolResult(
                ok=False,
                content="",
                error_message=f"skill not found: {skill_name!r}",
            )

        body_path_str = str(spec.body_path)
        skill_dir_str = str(spec.body_path.parent)

        # Step 3: emit skill.invoked。
        await self._emit(
            "skill.invoked",
            {
                "name": spec.name,
                "source": spec.source,
                "body_path": body_path_str,
                "args": args_value,
                "body_chars": 0,
            },
            ctx,
        )

        t0 = time.monotonic()

        # Step 4: 读 body。
        try:
            raw = await asyncio.to_thread(
                spec.body_path.read_text,
                encoding="utf-8",
            )
        except (OSError, UnicodeDecodeError) as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit(
                "skill.failed",
                {
                    "name": spec.name,
                    "source": spec.source,
                    "error_kind": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
                ctx,
            )
            return ToolResult(
                ok=False,
                content="",
                error_message=f"skill body read failed: {exc}",
            )

        # Step 5: !command 检测。
        try:
            assert_no_command_substitution(raw)
        except SkillSecurityError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit(
                "skill.failed",
                {
                    "name": spec.name,
                    "source": spec.source,
                    "error_kind": "SkillSecurityError",
                    "message": exc.message,
                    "elapsed_ms": elapsed_ms,
                },
                ctx,
            )
            return ToolResult(
                ok=False,
                content="",
                error_message=exc.message,
            )

        # Step 6: 单次变量替换。
        try:
            substituted = substitute_vars(
                raw,
                args=args_value,
                skill_dir=skill_dir_str,
                session_id=ctx.session_id,
            )
        except Exception as exc:
            # 兜底：substitute_vars 当前实现纯 str.replace 不会抛，
            # 但保留异常路径以防未来扩展，避免 emit 漏字段。
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            await self._emit(
                "skill.failed",
                {
                    "name": spec.name,
                    "source": spec.source,
                    "error_kind": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": elapsed_ms,
                },
                ctx,
            )
            return ToolResult(
                ok=False,
                content="",
                error_message=f"skill variable substitution failed: {exc}",
            )

        # Step 7: emit skill.completed + 返回 ToolResult(ok=True)。
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        body_chars = len(substituted)
        await self._emit(
            "skill.completed",
            {
                "name": spec.name,
                "source": spec.source,
                "body_chars": body_chars,
                "elapsed_ms": elapsed_ms,
            },
            ctx,
        )
        return ToolResult(
            ok=True,
            content=substituted,
            data={
                "name": spec.name,
                "source": spec.source,
                "body_path": body_path_str,
                "body_chars": body_chars,
                "elapsed_ms": elapsed_ms,
            },
        )

    async def _emit(
        self,
        kind: str,
        payload: dict[str, Any],
        ctx: ToolContext,
    ) -> None:
        """fan-out 单条事件到所有 sink，per-sink try/except 隔离。

        与 :func:`memory.safety_write._emit_write_event` 模式一致：
        sink 抛异常不污染主链路，也不打断对其他 sink 的 fan-out。
        """
        if not self._sinks:
            return
        event = Event(
            kind=kind,
            run_id=ctx.run_id,
            turn=ctx.turn,
            payload=payload,
        )
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception:
                # sink 异常不污染主链路，也不打断对其他 sink 的 fan-out
                continue


__all__ = [
    "SKILL_TOOL_SCHEMA",
    "SkillSecurityError",
    "SkillTool",
    "assert_no_command_substitution",
    "substitute_vars",
]
