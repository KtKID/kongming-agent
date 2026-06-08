from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import is_dataclass
from typing import Any, cast

import pytest

from application.agent_workflows.strategies.map_reduce.contracts import (
    MapperOutputValidator,
    MapReduceContractError,
    parse_map_reduce_workflow_spec,
    validate_mapper_output,
)


def _workflow_payload() -> dict[str, Any]:
    return {
        "mode": "map_reduce",
        "objective": "Find runtime contract risks in agent workflow code.",
        "input_source": {
            "kind": "path_glob",
            "root_dir": ".",
            "include": ["src/**/*.py", "tests/**/*.py"],
            "exclude": [".venv/**", "web/node_modules/**"],
            "files": [],
            "index_provider": "rg",
            "input_digest": "sha256:input",
        },
        "shard_strategy": {
            "kind": "by_file_count",
            "max_files_per_shard": 8,
            "max_estimated_tokens_per_shard": 20000,
            "min_shards": 1,
            "max_shards": 12,
            "preserve_directory_boundary": True,
            "prefer_dependency_cohesion": False,
        },
        "output_contract": "code_findings",
        "mapper": {
            "name_prefix": "map-contract",
            "prompt_template": "code_findings_v0_1",
            "tool_names": ["read_file", "list_dir", "write_file"],
            "skill_names": ["x-proj-research"],
            "permission_mode": "scoped_workdir",
            "max_turns": 3,
            "max_output_chars": 60000,
        },
        "reducer": {
            "kind": "deterministic",
            "dedupe_strategy": "exact_dedupe_key",
            "ranking_strategy": "severity_first",
            "max_findings": 50,
            "include_failed_shards": True,
            "reducer_prompt_template": None,
        },
        "limits": {
            "max_concurrency": 4,
            "workflow_timeout_seconds": 1800,
            "mapper_timeout_seconds": 300,
            "reducer_timeout_seconds": 300,
            "mapper_retries": 1,
            "validation_repair_retries": 0,
        },
        "audit_tags": ["task:map-reduce-contracts-v0.1", "unit"],
    }


def _mapper_output_payload(
    *,
    status: str = "completed",
    mapper_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "output_contract": "code_findings",
        "shard_id": "shard-001",
        "status": status,
        "summary": "One architecture finding found.",
        "files_seen": ["src/executors/agent_runtime/workflow/manager.py"],
        "findings": [
            {
                "dedupe_key": "agent-workflow-manager:strategy-registry",
                "title": "Workflow mode dispatch is hard-coded",
                "category": "architecture",
                "severity": "P1",
                "confidence": 0.82,
                "locations": [
                    {
                        "path": "src/executors/agent_runtime/workflow/manager.py",
                        "line_start": 34,
                        "line_end": 34,
                        "symbol": "WorkflowMode",
                        "excerpt": 'WorkflowMode = Literal["parallel"]',
                    }
                ],
                "evidence": "WorkflowMode only contains the current parallel mode.",
                "rationale": "map_reduce needs a runtime dispatch contract.",
                "recommendation": "Add strategy registry dispatch before adding planner code.",
                "impact_area": ["runtime", "api"],
                "source_shard_id": "shard-001",
            }
        ],
        "coverage": {
            "files_assigned": 2,
            "files_seen_count": 1,
            "symbols_seen_count": 3,
            "skipped_files": ["src/executors/agent_runtime/native_runtime.py"],
            "skip_reasons": ["out_of_scope_for_contract_test"],
        },
        "errors": mapper_errors or [],
    }


def _issue_value(issue: Any, field: str) -> Any:
    if isinstance(issue, Mapping):
        return issue[field]
    return getattr(issue, field)


def _normalize_issue(issue: Any) -> dict[str, str]:
    path = _issue_value(issue, "path")
    code = _issue_value(issue, "code")
    message = _issue_value(issue, "message")
    assert isinstance(path, str) and path
    assert isinstance(code, str) and code
    assert isinstance(message, str) and message
    return {"path": path, "code": code, "message": message}


def _exception_errors(exc: MapReduceContractError) -> tuple[Any, ...]:
    errors = getattr(exc, "errors")
    if callable(errors):
        errors = errors()
    return tuple(errors)


def _assert_issue(
    errors: tuple[Any, ...] | list[Any],
    *,
    path: str,
    code: str,
    message_contains: str,
) -> None:
    normalized = [_normalize_issue(error) for error in errors]
    assert any(
        error["path"] == path and error["code"] == code and message_contains in error["message"]
        for error in normalized
    ), normalized


def test_parse_map_reduce_workflow_spec_accepts_v01_payload_and_normalizes_tuple_fields() -> None:
    spec = parse_map_reduce_workflow_spec(_workflow_payload())

    assert is_dataclass(spec)
    assert spec.mode == "map_reduce"
    assert spec.objective == "Find runtime contract risks in agent workflow code."
    assert spec.output_contract == "code_findings"

    assert is_dataclass(spec.input_source)
    assert spec.input_source.kind == "path_glob"
    assert spec.input_source.include == ("src/**/*.py", "tests/**/*.py")
    assert spec.input_source.exclude == (".venv/**", "web/node_modules/**")
    assert spec.input_source.files == ()

    assert is_dataclass(spec.shard_strategy)
    assert spec.shard_strategy.kind == "by_file_count"
    assert spec.shard_strategy.prefer_dependency_cohesion is False

    assert is_dataclass(spec.mapper)
    assert spec.mapper.tool_names == ("read_file", "list_dir", "write_file")
    assert spec.mapper.skill_names == ("x-proj-research",)

    assert is_dataclass(spec.reducer)
    assert spec.reducer.kind == "deterministic"

    assert is_dataclass(spec.limits)
    assert spec.audit_tags == ("task:map-reduce-contracts-v0.1", "unit")


def test_parse_map_reduce_workflow_spec_rejects_dependency_graph_input_source() -> None:
    payload = _workflow_payload()
    payload["input_source"]["kind"] = "dependency_graph"

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path="$.input_source.kind",
        code="literal_error",
        message_contains="path_glob",
    )


def test_parse_map_reduce_workflow_spec_rejects_agent_assisted_reducer() -> None:
    payload = _workflow_payload()
    payload["reducer"]["kind"] = "agent_assisted"

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path="$.reducer.kind",
        code="literal_error",
        message_contains="deterministic",
    )


def test_parse_map_reduce_workflow_spec_rejects_non_v01_shard_strategy() -> None:
    payload = _workflow_payload()
    payload["shard_strategy"]["kind"] = "by_module"

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path="$.shard_strategy.kind",
        code="literal_error",
        message_contains="by_file_count",
    )


def test_parse_map_reduce_workflow_spec_rejects_dependency_cohesion_in_v01() -> None:
    payload = _workflow_payload()
    payload["shard_strategy"]["prefer_dependency_cohesion"] = True

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path="$.shard_strategy.prefer_dependency_cohesion",
        code="literal_error",
        message_contains="false",
    )


def test_parse_map_reduce_workflow_spec_rejects_wrong_output_contract() -> None:
    payload = _workflow_payload()
    payload["output_contract"] = "code_inventory"

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path="$.output_contract",
        code="literal_error",
        message_contains="code_findings",
    )


@pytest.mark.parametrize(
    ("case", "path", "message_contains"),
    [
        ("absolute_root", "$.input_source.root_dir", "relative"),
        ("traversal_include", "$.input_source.include[0]", "traversal"),
        ("empty_exclude", "$.input_source.exclude[0]", "non-empty"),
        ("absolute_file", "$.input_source.files[0]", "relative"),
        ("glob_file", "$.input_source.files[0]", "glob"),
        ("control_char_file", "$.input_source.files[0]", "control"),
    ],
)
def test_parse_map_reduce_workflow_spec_rejects_unsafe_input_paths(
    case: str,
    path: str,
    message_contains: str,
) -> None:
    payload = _workflow_payload()
    if case == "absolute_root":
        payload["input_source"]["root_dir"] = "/tmp/repo"
    elif case == "traversal_include":
        payload["input_source"]["include"] = ["../src/**/*.py"]
    elif case == "empty_exclude":
        payload["input_source"]["exclude"] = [""]
    elif case == "absolute_file":
        payload["input_source"]["files"] = ["/etc/passwd"]
    elif case == "glob_file":
        payload["input_source"]["files"] = ["src/*.py"]
    elif case == "control_char_file":
        payload["input_source"]["files"] = ["src/\x00secret.py"]
    else:  # pragma: no cover - protects the parametrized test table.
        raise AssertionError(f"unhandled case: {case}")

    with pytest.raises(MapReduceContractError) as exc_info:
        parse_map_reduce_workflow_spec(payload)

    _assert_issue(
        _exception_errors(exc_info.value),
        path=path,
        code="path_error",
        message_contains=message_contains,
    )


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(_mapper_output_payload()),
        "```json\n" + json.dumps(_mapper_output_payload(), indent=2) + "\n```",
        (
            "Mapper completed the shard. Structured output follows:\n"
            + json.dumps(_mapper_output_payload(), indent=2)
            + "\nEnd of mapper output."
        ),
    ],
)
def test_mapper_output_validator_accepts_pure_fenced_and_wrapped_json(content: str) -> None:
    result = MapperOutputValidator().validate(content)

    assert result.valid is True
    assert result.errors == ()
    assert result.output is not None
    assert result.output.output_contract == "code_findings"
    assert result.output.shard_id == "shard-001"


@pytest.mark.parametrize(
    ("case", "path", "code", "message_contains"),
    [
        ("missing_findings", "$.findings", "type_error", "expected array"),
        ("invalid_severity", "$.findings[0].severity", "literal_error", "expected one of"),
        (
            "confidence_out_of_range",
            "$.findings[0].confidence",
            "value_error",
            "between 0.0 and 1.0",
        ),
        ("wrong_output_contract", "$.output_contract", "literal_error", "code_findings"),
        ("missing_errors", "$.errors", "type_error", "expected array"),
        ("empty_locations", "$.findings[0].locations", "value_error", "at least one"),
    ],
)
def test_mapper_output_validator_returns_structured_errors_for_invalid_output(
    case: str,
    path: str,
    code: str,
    message_contains: str,
) -> None:
    payload = _mapper_output_payload()
    if case == "missing_findings":
        del payload["findings"]
    elif case == "invalid_severity":
        payload["findings"][0]["severity"] = "P4"
    elif case == "confidence_out_of_range":
        payload["findings"][0]["confidence"] = 1.4
    elif case == "wrong_output_contract":
        payload["output_contract"] = "code_inventory"
    elif case == "missing_errors":
        del payload["errors"]
    elif case == "empty_locations":
        payload["findings"][0]["locations"] = []
    else:  # pragma: no cover - protects the parametrized test table.
        raise AssertionError(f"unhandled case: {case}")

    result = MapperOutputValidator().validate(json.dumps(payload))

    assert result.valid is False
    assert result.output is None
    assert result.errors
    _assert_issue(result.errors, path=path, code=code, message_contains=message_contains)


def test_mapper_output_validator_converts_code_findings_envelope_to_dataclasses() -> None:
    payload = _mapper_output_payload(
        status="partial",
        mapper_errors=[
            {
                "error_type": "tool_error",
                "message": "read_file failed for one assigned file",
                "file_path": "src/executors/agent_runtime/native_runtime.py",
                "retryable": True,
            }
        ],
    )

    result = MapperOutputValidator().validate(json.dumps(payload))

    assert result.valid is True
    assert result.errors == ()
    envelope = result.output
    assert envelope is not None
    assert is_dataclass(envelope)

    assert envelope.files_seen == ("src/executors/agent_runtime/workflow/manager.py",)
    finding = envelope.findings[0]
    assert is_dataclass(finding)
    assert finding.severity == "P1"
    assert finding.impact_area == ("runtime", "api")

    location = finding.locations[0]
    assert is_dataclass(location)
    assert location.path == "src/executors/agent_runtime/workflow/manager.py"
    assert location.line_start == 34

    assert is_dataclass(envelope.coverage)
    assert envelope.coverage.skipped_files == ("src/executors/agent_runtime/native_runtime.py",)
    assert envelope.coverage.skip_reasons == ("out_of_scope_for_contract_test",)

    mapper_error = envelope.errors[0]
    assert is_dataclass(mapper_error)
    assert mapper_error.error_type == "tool_error"
    assert mapper_error.retryable is True


def test_validate_mapper_output_rejects_shard_mismatch_with_expected_context() -> None:
    result = validate_mapper_output(
        json.dumps(_mapper_output_payload()),
        expected_shard_id="shard-expected",
    )

    assert result.valid is False
    assert result.output is None
    assert result.expected_shard_id == "shard-expected"
    assert result.shard_id == "shard-001"
    assert result.raw_content_digest.startswith("sha256:")
    _assert_issue(
        result.errors,
        path="$.shard_id",
        code="shard_mismatch",
        message_contains="does not match",
    )


def test_validate_mapper_output_rejects_cross_shard_finding_source() -> None:
    payload = _mapper_output_payload()
    payload["findings"][0]["source_shard_id"] = "shard-other"

    result = validate_mapper_output(json.dumps(payload), expected_shard_id="shard-001")

    assert result.valid is False
    assert result.output is None
    assert result.shard_id == "shard-001"
    _assert_issue(
        result.errors,
        path="$.findings[0].source_shard_id",
        code="shard_mismatch",
        message_contains="must match",
    )


def test_validate_mapper_output_returns_read_only_payload_snapshot() -> None:
    result = validate_mapper_output(
        json.dumps(_mapper_output_payload()), expected_shard_id="shard-001"
    )

    assert result.valid is True
    assert result.payload is not None
    payload = cast(dict[str, Any], result.payload)
    with pytest.raises(TypeError):
        payload["shard_id"] = "mutated"
    coverage = cast(dict[str, Any], result.payload["coverage"])
    with pytest.raises(TypeError):
        coverage["files_assigned"] = 99
    finding = cast(dict[str, Any], result.payload["findings"][0])
    with pytest.raises(TypeError):
        finding["severity"] = "P0"
    assert isinstance(result.payload["files_seen"], tuple)
    assert isinstance(result.payload["findings"], tuple)


def test_validate_mapper_output_keeps_expected_shard_when_json_parse_fails() -> None:
    result = validate_mapper_output("mapper failed before json", expected_shard_id="shard-404")

    assert result.valid is False
    assert result.output is None
    assert result.payload is None
    assert result.expected_shard_id == "shard-404"
    assert result.shard_id is None
    assert result.raw_content_digest.startswith("sha256:")
    _assert_issue(
        result.errors,
        path="$",
        code="json_parse_failed",
        message_contains="failed to parse JSON",
    )
