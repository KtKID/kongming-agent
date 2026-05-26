from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "run_with_timing.py"
_LOG_DIR = _REPO_ROOT / ".kongming" / "test-logs"
_INDEX_FILE = _LOG_DIR / "index.jsonl"


def _run(*command: str, label: str = "wrapper-test", cwd: str = ".") -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--label",
            label,
            "--cwd",
            cwd,
            "--",
            *command,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _latest_log(stem: str) -> Path | None:
    if not _LOG_DIR.exists():
        return None
    candidates = sorted(_LOG_DIR.glob(f"{stem}-*.log"))
    return candidates[-1] if candidates else None


def _latest_index_record(label: str) -> dict | None:
    if not _INDEX_FILE.exists():
        return None
    records = [
        json.loads(line) for line in _INDEX_FILE.read_text(encoding="utf-8").splitlines() if line
    ]
    matched = [record for record in records if record["label"] == label]
    return matched[-1] if matched else None


def test_script_writes_log_for_passing_command() -> None:
    exit_code, output = _run(
        sys.executable,
        "-c",
        "print('timing-ok')",
        label="timing-pass",
    )
    assert exit_code == 0, output

    log = _latest_log("timing-pass")
    assert log is not None and log.is_file(), f"log not created in {_LOG_DIR}"

    text = log.read_text(encoding="utf-8")
    assert "=== run_with_timing.py ===" in text
    assert "command:" in text
    assert "started_utc:" in text
    assert "started_ms:" in text
    assert "timing-ok" in text
    assert "ended_utc:" in text
    assert "ended_ms:" in text
    assert "duration_ms:" in text
    assert "exit_code: 0" in text


def test_script_filename_format_uses_label_and_timestamp() -> None:
    _run(sys.executable, "-c", "print('x')", label="timing-file-name")

    log = _latest_log("timing-file-name")
    assert log is not None
    assert re.match(r"^timing-file-name-\d{8}-\d{6}\.log$", log.name), log.name


def test_script_propagates_exit_code_on_failure() -> None:
    exit_code, output = _run(sys.executable, "-c", "import sys; sys.exit(7)", label="timing-fail")
    assert exit_code == 7, output

    log = _latest_log("timing-fail")
    assert log is not None
    text = log.read_text(encoding="utf-8")
    assert "ended_utc:" in text
    assert "exit_code: 7" in text


def test_script_appends_machine_readable_timing_index() -> None:
    exit_code, _ = _run(sys.executable, "-c", "print('index-ok')", label="timing-index")
    assert exit_code == 0

    record = _latest_index_record("timing-index")
    assert record is not None
    assert record["label"] == "timing-index"
    assert sys.executable in record["command"]
    assert record["exit_code"] == 0
    assert record["ended_ms"] >= record["started_ms"]
    assert record["duration_ms"] == record["ended_ms"] - record["started_ms"]
    assert record["duration_ms"] >= 0
    assert record["log_file"].endswith(".log")


def test_script_honors_cwd_override() -> None:
    exit_code, _ = _run(
        sys.executable,
        "-c",
        "from pathlib import Path; print(Path.cwd().name)",
        label="timing-cwd",
        cwd="tests",
    )
    assert exit_code == 0

    log = _latest_log("timing-cwd")
    assert log is not None
    text = log.read_text(encoding="utf-8")
    assert "tests" in text
