"""CLI workflow smoke 进程级回归测试。

本脚本负责启动真实 CLI 进程并验证 workflow 本地日志。
作用是覆盖 unit helper 无法发现的 CLI 装配、环境变量覆盖、workflow tool
入口和 audit.jsonl 落盘链路。
关键执行流程：创建隔离 KONGMING_HOME，运行 `python -m hosts.cli.main
--workflow-smoke`，读取本次 workflow 的 audit.jsonl，并断言 resolved runtime
字段写入真实日志。
关键函数：test_cli_workflow_smoke_writes_runtime_to_real_audit_log 验证端到端
链路，_run_cli_workflow_smoke 启动 CLI，_find_single_audit_log 定位审计日志。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def test_cli_workflow_smoke_writes_runtime_to_real_audit_log(tmp_path: Path) -> None:
    """启动真实 CLI workflow smoke，输入为隔离 home，输出为带 resolved runtime 的 audit.jsonl。"""
    home = tmp_path / "kongming-home"

    result = _run_cli_workflow_smoke(home)

    assert result.returncode == 0, result.stderr
    assert "[workflow-smoke] ok" in result.stdout

    audit_log = _find_single_audit_log(home)
    records = _jsonl(audit_log)

    assert records[0]["action"] == "map_reduce_started"
    runtime_payload = records[0]["payload"]["resolved_runtime"]
    assert runtime_payload["model"]
    assert "field_sources" in runtime_payload


def _run_cli_workflow_smoke(home: Path) -> subprocess.CompletedProcess[str]:
    """启动 CLI smoke，输入为隔离 home，输出为 subprocess 完整结果。"""
    repo_root = Path(__file__).resolve().parents[2]
    src_path = repo_root / "src"
    env = os.environ.copy()
    env["KONGMING_HOME"] = str(home)
    env["KONGMING_APPROVAL_MODE"] = "auto_allow"
    env["KONGMING_MODEL_NAME"] = "cli-workflow-smoke"
    env["KONGMING_MODEL_BASE_URL"] = "http://127.0.0.1:9/v1"
    env["KONGMING_MODEL_API_KEY"] = ""
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = (
        str(src_path) if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    )

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hosts.cli.main",
            "--workflow-smoke",
            "--no-trace",
        ],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _find_single_audit_log(home: Path) -> Path:
    """定位 CLI workflow audit 日志，输入为 KONGMING_HOME，输出为唯一 audit.jsonl。"""
    audit_logs = sorted(
        (home / "sessions" / "workflow-smoke").glob("agent-workflows/*/audit.jsonl")
    )
    assert len(audit_logs) == 1
    return audit_logs[0]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 文件，输入为路径，输出为每行 JSON 对象列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
