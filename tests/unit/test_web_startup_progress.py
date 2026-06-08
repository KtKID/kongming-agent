"""startup_progress.py 单测。

覆盖点：

1. report() 写出合法 JSON，pct 正确
2. report() 未知 step_id 静默跳过
3. report() 后续步骤自动标记前序为 done
4. done() 设置 status=done, pct=100
5. fail() 设置 status=error + error 字段
6. cleanup() 删除文件
7. 完整生命周期：report → done → cleanup
8. 错误路径生命周期：report → fail → 文件保留
"""

from __future__ import annotations

import json
from pathlib import Path

from web.app_support.startup_progress import STARTUP_STEPS, StartupProgress


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


def test_report_writes_valid_json_with_correct_pct(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")

    path = home / "web" / "startup.json"
    assert path.exists()
    data = _read_json(path)
    assert data["version"] == 1
    assert data["current_step"] == "env"
    assert data["pct"] == 5
    assert data["status"] == "running"
    assert data["error"] is None
    assert isinstance(data["updated_at"], int)
    assert len(data["steps"]) == len(STARTUP_STEPS)


def test_report_unknown_step_id_is_noop(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("nonexistent_step")

    path = home / "web" / "startup.json"
    assert not path.exists()


def test_report_marks_reported_steps_as_done(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")
    sp.report("frontend")

    data = _read_json(home / "web" / "startup.json")
    steps_by_id = {s["id"]: s for s in data["steps"]}
    assert steps_by_id["env"]["status"] == "done"  # explicitly reported
    assert steps_by_id["port"]["status"] == "pending"  # never reported
    assert steps_by_id["frontend"]["status"] == "running"
    assert steps_by_id["imports"]["status"] == "pending"


def test_report_latest_pct_wins(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")
    sp.report("config")

    data = _read_json(home / "web" / "startup.json")
    assert data["pct"] == 55
    assert data["current_step"] == "config"


# ---------------------------------------------------------------------------
# done()
# ---------------------------------------------------------------------------


def test_done_sets_status_done_pct_100(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")
    sp.done()

    data = _read_json(home / "web" / "startup.json")
    assert data["status"] == "done"
    assert data["pct"] == 100
    assert data["current_step"] == "ready"
    for s in data["steps"]:
        assert s["status"] == "done"


# ---------------------------------------------------------------------------
# fail()
# ---------------------------------------------------------------------------


def test_fail_sets_error_status(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("frontend")
    sp.fail("build crashed")

    data = _read_json(home / "web" / "startup.json")
    assert data["status"] == "error"
    assert data["error"] == "build crashed"
    assert data["pct"] == 40  # frontend=40 是已 reported 的最大 pct


def test_fail_preserves_last_reported_pct(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")
    sp.report("factory")
    sp.fail("something broke")

    data = _read_json(home / "web" / "startup.json")
    assert data["pct"] == 60  # factory=60 是已 reported 的最大 pct


# ---------------------------------------------------------------------------
# cleanup()
# ---------------------------------------------------------------------------


def test_cleanup_removes_file(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("env")

    path = home / "web" / "startup.json"
    assert path.exists()
    sp.cleanup()
    assert not path.exists()


def test_cleanup_noop_when_file_missing(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# 完整生命周期
# ---------------------------------------------------------------------------


def test_full_lifecycle_success(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)

    for step in STARTUP_STEPS:
        sp.report(step["id"])  # type: ignore[arg-type]

    sp.done()
    sp.cleanup()

    assert not (home / "web" / "startup.json").exists()


def test_full_lifecycle_error(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)

    sp.report("env")
    sp.report("frontend")
    sp.fail("npm build failed")

    path = home / "web" / "startup.json"
    assert path.exists()
    data = _read_json(path)
    assert data["status"] == "error"
    assert data["error"] == "npm build failed"


# ---------------------------------------------------------------------------
# JSON schema 完整性
# ---------------------------------------------------------------------------


def test_json_schema_has_all_required_fields(tmp_path: Path) -> None:
    home = tmp_path / ".kongming"
    sp = StartupProgress(home)
    sp.report("app")

    data = _read_json(home / "web" / "startup.json")
    assert set(data.keys()) == {
        "version",
        "steps",
        "current_step",
        "pct",
        "status",
        "error",
        "updated_at",
    }
    for s in data["steps"]:
        assert set(s.keys()) == {"id", "label", "pct", "status"}
