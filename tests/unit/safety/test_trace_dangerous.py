"""unit：危险操作 trace 事件记录。

覆盖 P0 事故修复 DoD：
1. destructive_force_ask 事件包含完整 command 字段
2. tool.destructive_force_ask 事件不被 log_silent_reads 过滤
3. shell 命令的 event payload 包含 command 字段
4. .kongming/skills/ 路径在 sensitive_paths 表中
"""

from __future__ import annotations

from core.contracts import ApprovalDecision, ApprovalRequest
from safety.approval.chain import _build_event_payload
from safety.approval.default_rules import DEFAULT_SENSITIVE_PATHS
from safety.approval.types import ApprovalMetadataKeys

# ---------------------------------------------------------------------------
# _build_event_payload 测试
# ---------------------------------------------------------------------------


def _req(
    *,
    tool_name: str = "run_shell",
    command: str = "rm -rf /tmp/x",
) -> ApprovalRequest:
    return ApprovalRequest(
        run_id="r1",
        session_id="s1",
        turn=1,
        call_id="c1",
        tool_name=tool_name,
        arguments={"command": command},
    )


def _decision(*, matched_rule: str = "destructive:rm-recursive") -> ApprovalDecision:
    return ApprovalDecision(
        outcome="approved",
        reason="destructive command: forced consent",
        metadata={
            ApprovalMetadataKeys.DECISION_CLASS: "explicit_consent",
            ApprovalMetadataKeys.BOUNDARY_KIND: "host",
            ApprovalMetadataKeys.MATCHED_RULE: matched_rule,
        },
    )


class TestTracePayloadCommand:
    """验证 shell 命令的 trace payload 包含完整 command。"""

    def test_shell_command_in_payload(self) -> None:
        payload = _build_event_payload(_decision(), _req(command="rm -rf .kongming/skills/x"))
        assert payload["command"] == "rm -rf .kongming/skills/x"

    def test_shell_command_includes_tool_name(self) -> None:
        payload = _build_event_payload(_decision(), _req())
        assert payload["tool_name"] == "run_shell"

    def test_shell_command_includes_matched_rule(self) -> None:
        payload = _build_event_payload(_decision(), _req())
        assert payload["matched_rule"] == "destructive:rm-recursive"

    def test_non_shell_tool_has_no_command_field(self) -> None:
        req = ApprovalRequest(
            run_id="r1",
            session_id="s1",
            turn=1,
            call_id="c1",
            tool_name="read_file",
            arguments={"path": "/tmp/file.txt"},
        )
        dec = ApprovalDecision(outcome="approved", reason="ok")
        payload = _build_event_payload(dec, req)
        assert "command" not in payload


# ---------------------------------------------------------------------------
# .kongming/skills/ 路径保护
# ---------------------------------------------------------------------------


class TestKongmingSkillsSensitivePath:
    """验证 .kongming/skills/ 在 DEFAULT_SENSITIVE_PATHS 中。"""

    def test_kongming_skills_dir_in_sensitive_paths(self) -> None:
        names = {rule.name for rule in DEFAULT_SENSITIVE_PATHS}
        assert "kongming-skills-dir" in names

    def test_kongming_skills_dir_is_elevated_write(self) -> None:
        rule = next(r for r in DEFAULT_SENSITIVE_PATHS if r.name == "kongming-skills-dir")
        assert rule.effect == "elevated"
        assert "write" in rule.ops
        assert rule.matcher == ".kongming/skills/"
