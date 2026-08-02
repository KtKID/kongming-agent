from __future__ import annotations

import pytest

from hosts.web.integrations.codex.approval import map_permission_mode


class TestPermissionModeMapping:
    def test_default(self) -> None:
        assert map_permission_mode("default") == ("workspace-write", "untrusted")

    def test_accept_edits(self) -> None:
        assert map_permission_mode("acceptEdits") == ("workspace-write", "never")

    def test_bypass_permissions(self) -> None:
        assert map_permission_mode("bypassPermissions") == ("danger-full-access", "never")

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported Codex permission mode"):
            map_permission_mode("unknown")

    def test_empty_string_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported Codex permission mode"):
            map_permission_mode("")
