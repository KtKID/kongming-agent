from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> tuple[int, str]:
    now = datetime.now(timezone.utc)
    return int(now.timestamp() * 1000), now.isoformat().replace("+00:00", "Z")


def _safe_label(label: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in label)


def _command_string(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="command")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("run_with_timing.py: missing command", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    workdir = (repo_root / args.cwd).resolve()
    log_dir = repo_root / ".kongming" / "test-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = _safe_label(args.label)
    log_file = log_dir / f"{safe_label}-{ts}.log"
    index_file = log_dir / "index.jsonl"

    started_ms, started_utc = _utc_now()
    command_str = _command_string(command)

    with log_file.open("w", encoding="utf-8", newline="") as fh:
        fh.write("=== run_with_timing.py ===\n")
        fh.write(f"label: {args.label}\n")
        fh.write(f"command: {command_str}\n")
        fh.write(f"started_utc: {started_utc}\n")
        fh.write(f"started_ms: {started_ms}\n")
        fh.write(f"cwd: {workdir}\n")
        fh.write(f"log_file: {log_file}\n")
        fh.write("===\n\n")
        fh.flush()

        proc = subprocess.Popen(
            command,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            fh.write(line)
        exit_code = proc.wait()

        ended_ms, ended_utc = _utc_now()
        duration_ms = ended_ms - started_ms
        fh.write("\n===\n")
        fh.write(f"ended_utc: {ended_utc}\n")
        fh.write(f"ended_ms: {ended_ms}\n")
        fh.write(f"duration_ms: {duration_ms}\n")
        fh.write(f"exit_code: {exit_code}\n")

    record = {
        "label": args.label,
        "command": command_str,
        "started_utc": started_utc,
        "started_ms": started_ms,
        "ended_utc": ended_utc,
        "ended_ms": ended_ms,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "cwd": str(workdir),
        "log_file": str(log_file),
    }
    with index_file.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")

    print(f"log_file: {log_file}")
    print(f"duration_ms: {duration_ms}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
