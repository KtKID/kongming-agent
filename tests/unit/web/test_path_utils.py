from web.app_support.path_utils import is_absolute_workspace_path


def test_accepts_posix_absolute_path() -> None:
    assert is_absolute_workspace_path("/Volumes/work/proj")


def test_accepts_windows_drive_absolute_path() -> None:
    assert is_absolute_workspace_path(r"E:\xgt\proj\agent-proj\kongming-agent")
    assert is_absolute_workspace_path("E:/xgt/proj/agent-proj/kongming-agent")


def test_accepts_windows_unc_absolute_path() -> None:
    assert is_absolute_workspace_path(r"\\server\share\proj")


def test_rejects_relative_paths() -> None:
    assert not is_absolute_workspace_path("relative/path")
    assert not is_absolute_workspace_path(r".\relative\path")
    assert not is_absolute_workspace_path(r"E:relative\path")
