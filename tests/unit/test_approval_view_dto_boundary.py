"""审批 view DTO 边界测试。

本测试保证宿主展示层只依赖 PendingApprovalView，不直接 import
ApprovalManager 内部的 _PendingApproval。关键流程是扫描宿主 sink 和 inbox
适配器源码，发现私有类型跨层引用即失败。
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_hosts_and_inbox_do_not_import_pending_approval_internal_state() -> None:
    """宿主和 inbox 适配器不得直接引用 manager 私有 pending 状态。"""
    checked_paths = [
        _ROOT / "src" / "hosts" / "cli" / "approval_manager_sink.py",
        _ROOT / "src" / "hosts" / "web" / "avatar" / "approval_sink.py",
        _ROOT / "src" / "safety" / "inbox" / "event_sink.py",
    ]

    offenders = [
        str(path.relative_to(_ROOT))
        for path in checked_paths
        if "_PendingApproval" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
