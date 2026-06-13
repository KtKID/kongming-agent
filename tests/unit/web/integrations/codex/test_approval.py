from __future__ import annotations

from hosts.web.integrations.codex.approval import map_permission_mode


class TestPermissionModeMapping:
    def test_default(self) -> None:
        assert map_permission_mode("default") == ("workspace-write", "untrusted")

    def test_accept_edits(self) -> None:
        assert map_permission_mode("acceptEdits") == ("workspace-write", "never")

    def test_bypass_permissions(self) -> None:
        assert map_permission_mode("bypassPermissions") == ("danger-full-access", "never")

    def test_unknown_mode_falls_back(self) -> None:
        assert map_permission_mode("unknown") == ("workspace-write", "untrusted")

    def test_empty_string_falls_back(self) -> None:
        assert map_permission_mode("") == ("workspace-write", "untrusted")
