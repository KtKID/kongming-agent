"""用户选择内置工具。

本模块提供 ``present_choices`` 工具，让模型在需要用户选择方案、范围、
偏好或下一步动作时，通过 EventSink 向 Web UI 发出选择请求。
关键流程：校验 title / description / questions / options，使用 ToolContext.call_id
作为 request_id，emit ``choice.requested`` 事件，并返回提示模型等待用户确认。
关键函数：ChoiceTool._run 执行校验和事件输出；build_choice_tool 构造工具实例。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.contracts import Event, EventSink, PreparedToolCall, ToolContext
from tools.runtime.base import BaseBuiltinTool

CUSTOM_OPTION_ID = "__custom__"


class ChoiceTool(BaseBuiltinTool):
    """向当前 Web 会话展示用户选择面板。"""

    name = "present_choices"
    title = "向用户展示方案选择"
    description = (
        "当需要用户在多个方案、范围、偏好或下一步动作之间做选择时调用。"
        "工具会在 Web 输入框上方展示多问题单选列表，每个问题固定包含一个自定义输入选项，"
        "用户确认后选择结果会回到当前对话。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "选择面板标题，说明这组问题的主题。",
            },
            "description": {
                "type": "string",
                "description": "面板说明，描述为什么需要用户选择以及选择结果将用于什么场景。",
            },
            "questions": {
                "type": "array",
                "description": "问题列表，按数组顺序逐题展示。",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "问题稳定 ID，同一次请求内唯一。",
                        },
                        "title": {
                            "type": "string",
                            "description": "问题标题，直接展示给用户。",
                        },
                        "description": {
                            "type": ["string", "null"],
                            "description": "问题补充说明，可为空。",
                        },
                        "options": {
                            "type": "array",
                            "description": "LLM 提供的候选选项。系统会固定追加自定义输入选项。",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "选项稳定 ID，同一问题内唯一。",
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": "选项标题，短文本。",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "选项说明，描述适用场景、取舍或影响。",
                                    },
                                    "value": {
                                        "type": "object",
                                        "description": "可选结构化值，原样回传给 LLM。",
                                    },
                                },
                                "required": ["id", "label", "description"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "title", "options"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "description", "questions"],
        "additionalProperties": False,
    }

    def __init__(self, *, event_sinks: Sequence[EventSink] = ()) -> None:
        super().__init__()
        self._event_sinks = tuple(event_sinks)

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验并冻结完整选择面板 payload。"""
        self._validate_args(arguments)
        request_id = context.call_id.strip()
        if not request_id:
            raise ValueError("ToolContext.call_id is required")
        return PreparedToolCall(
            arguments={
                **self._validate_payload(arguments),
                "request_id": request_id,
            }
        )

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        payload = args
        request_id = args["request_id"]

        event_payload = {
            "request_id": request_id,
            "title": payload["title"],
            "description": payload["description"],
            "questions": payload["questions"],
        }
        event = Event(
            kind="choice.requested",
            run_id=ctx.run_id,
            turn=ctx.turn,
            payload=event_payload,
        )
        for sink in self._event_sinks:
            try:
                await sink.emit(event)
            except Exception:
                continue

        data = {
            "request_id": request_id,
            "title": payload["title"],
            "question_count": len(payload["questions"]),
            "custom_option_id": CUSTOM_OPTION_ID,
        }
        return (
            "已向用户展示选择面板，请等待用户在界面中完成选择并确认。",
            data,
        )

    def _validate_payload(self, args: dict[str, Any]) -> dict[str, Any]:
        unknown_top_keys = sorted(set(args) - {"title", "description", "questions"})
        if unknown_top_keys:
            raise ValueError(f"unknown top-level fields: {unknown_top_keys}")

        title = self._required_str(args, "title")
        description = self._required_str(args, "description")
        raw_questions = args.get("questions")
        if not isinstance(raw_questions, list):
            raise ValueError("questions must be a list")
        if not raw_questions:
            raise ValueError("questions must contain at least one item")

        questions: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for q_index, raw_question in enumerate(raw_questions):
            if not isinstance(raw_question, dict):
                raise ValueError(f"questions[{q_index}] must be an object")
            unknown_question_keys = sorted(
                set(raw_question) - {"id", "title", "description", "options"}
            )
            if unknown_question_keys:
                raise ValueError(
                    f"questions[{q_index}] has unknown fields: {unknown_question_keys}"
                )
            question_id = self._required_str(raw_question, "id", path=f"questions[{q_index}]")
            if question_id in seen_question_ids:
                raise ValueError(f"duplicate question id: {question_id}")
            seen_question_ids.add(question_id)

            question_title = self._required_str(
                raw_question,
                "title",
                path=f"questions[{q_index}]",
            )
            question_description = self._optional_str(
                raw_question,
                "description",
                path=f"questions[{q_index}]",
            )
            options = self._validate_options(
                raw_question.get("options"),
                question_index=q_index,
            )
            questions.append(
                {
                    "id": question_id,
                    "title": question_title,
                    "description": question_description,
                    "options": options,
                }
            )

        return {
            "title": title,
            "description": description,
            "questions": questions,
        }

    def _validate_options(
        self,
        raw_options: Any,
        *,
        question_index: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_options, list):
            raise ValueError(f"questions[{question_index}].options must be a list")
        if not raw_options:
            raise ValueError(f"questions[{question_index}].options must contain at least one item")

        options: list[dict[str, Any]] = []
        seen_option_ids: set[str] = set()
        for option_index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, dict):
                raise ValueError(
                    f"questions[{question_index}].options[{option_index}] must be an object"
                )
            unknown_option_keys = sorted(set(raw_option) - {"id", "label", "description", "value"})
            if unknown_option_keys:
                raise ValueError(
                    "questions"
                    f"[{question_index}].options[{option_index}] has unknown fields: "
                    f"{unknown_option_keys}"
                )
            option_path = f"questions[{question_index}].options[{option_index}]"
            option_id = self._required_str(raw_option, "id", path=option_path)
            if option_id == CUSTOM_OPTION_ID:
                raise ValueError(f"{option_path}.id uses reserved option id: {CUSTOM_OPTION_ID}")
            if option_id in seen_option_ids:
                raise ValueError(
                    f"questions[{question_index}] has duplicate option id: {option_id}"
                )
            seen_option_ids.add(option_id)

            option: dict[str, Any] = {
                "id": option_id,
                "label": self._required_str(raw_option, "label", path=option_path),
                "description": self._required_str(
                    raw_option,
                    "description",
                    path=option_path,
                ),
            }
            if "value" in raw_option:
                value = raw_option["value"]
                if not isinstance(value, dict):
                    raise ValueError(f"{option_path}.value must be an object")
                option["value"] = value
            options.append(option)
        return options

    def _required_str(
        self,
        raw: dict[str, Any],
        key: str,
        *,
        path: str | None = None,
    ) -> str:
        value = raw.get(key)
        label = f"{path}.{key}" if path else key
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        return value.strip()

    def _optional_str(
        self,
        raw: dict[str, Any],
        key: str,
        *,
        path: str,
    ) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{path}.{key} must be a string")
        stripped = value.strip()
        return stripped or None


def build_choice_tool(*, event_sinks: Sequence[EventSink] = ()) -> ChoiceTool:
    """构造用户选择工具。"""
    return ChoiceTool(event_sinks=event_sinks)


__all__ = [
    "CUSTOM_OPTION_ID",
    "ChoiceTool",
    "build_choice_tool",
]
