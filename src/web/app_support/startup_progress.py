"""启动进度上报 — 写 ``.kongming/web/startup.json`` 供外部消费者（Tauri）轮询。

父子进程（ctl.py → run.py → app.py lifespan）各在关键步骤调用
:func:`StartupProgress.report`，文件通过 ``os.replace`` 原子写入，
避免读端拿到半写 JSON。

启动成功后 ``cleanup()`` 删除文件；失败则保留供消费者读取 error。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_pid = os.getpid()

__all__ = ["STARTUP_STEPS", "StartupProgress"]

logger = logging.getLogger(__name__)

# 步骤定义：id → (label, pct)。pct 为该步骤完成时的累计百分比。
STARTUP_STEPS: list[dict[str, object]] = [
    {"id": "env", "label": "加载环境配置", "pct": 5},
    {"id": "port", "label": "检查端口", "pct": 10},
    {"id": "frontend", "label": "构建前端", "pct": 40},
    {"id": "imports", "label": "加载模块", "pct": 50},
    {"id": "config", "label": "读取配置", "pct": 55},
    {"id": "factory", "label": "装配运行时", "pct": 60},
    {"id": "app", "label": "创建应用", "pct": 70},
    {"id": "uvicorn", "label": "绑定端口", "pct": 80},
    {"id": "lifespan", "label": "启动线程管理器", "pct": 90},
    {"id": "ready", "label": "服务就绪", "pct": 100},
]

_STEP_MAP: dict[str, int] = {s["id"]: s["pct"] for s in STARTUP_STEPS}  # type: ignore[misc]


class StartupProgress:
    """管理 startup.json 的读写。

    Args:
        home: ``.kongming/`` 根目录。
    """

    def __init__(self, home: Path) -> None:
        self._path = home / "web" / "startup.json"
        self._reported: set[str] = set()

    # -- public API --

    def report(self, step_id: str, *, status: str = "running", error: str | None = None) -> None:
        """更新到指定步骤。"""
        if step_id not in _STEP_MAP:
            logger.warning("startup_progress: unknown step_id=%r, skipping", step_id)
            return
        self._reported.add(step_id)
        self._write(self._build_payload(current_step=step_id, status=status, error=error))

    def done(self) -> None:
        """标记启动完成（pct=100, status='done'）。"""
        for s in STARTUP_STEPS:
            self._reported.add(s["id"])  # type: ignore[arg-type]
        self._write(self._build_payload(current_step="ready", status="done"))

    def fail(self, error: str) -> None:
        """标记启动失败。"""
        self._write(self._build_payload(status="error", error=error))

    def cleanup(self) -> None:
        """删除 startup.json。"""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            logger.debug("startup_progress: cleanup failed", exc_info=True)

    # -- internals --

    def _build_payload(
        self,
        *,
        current_step: str | None = None,
        status: str = "running",
        error: str | None = None,
    ) -> dict[str, object]:
        # 合并已有文件中的已完成步骤（跨进程场景：run.py 读 ctl.py 写的文件）
        prev_done: set[str] = set()
        try:
            raw = self._path.read_text(encoding="utf-8")
            prev = json.loads(raw)
            for entry in prev.get("steps", []):
                if entry.get("status") == "done":
                    prev_done.add(entry["id"])
        except (OSError, json.JSONDecodeError, KeyError):
            pass

        all_reported = self._reported | prev_done

        steps_out: list[dict[str, object]] = []
        for s in STARTUP_STEPS:
            sid: str = s["id"]  # type: ignore[assignment]
            if sid == current_step:
                step_status = status
            elif sid in all_reported:
                step_status = "done"
            else:
                step_status = "pending"
            steps_out.append(
                {"id": sid, "label": s["label"], "pct": s["pct"], "status": step_status}
            )

        if current_step and current_step in _STEP_MAP:
            pct = _STEP_MAP[current_step]
        elif status == "done":
            pct = 100
        elif status == "error":
            # 错误时保持最近一次 reported 的 pct（含跨进程已完成步骤）
            pct = max((_STEP_MAP[s] for s in all_reported if s in _STEP_MAP), default=0)
        else:
            pct = 0

        return {
            "version": 1,
            "steps": steps_out,
            "current_step": current_step,
            "pct": pct,
            "status": status,
            "error": error,
            "updated_at": int(time.time() * 1000),
        }

    def _write(self, data: dict[str, object]) -> None:
        """原子写：先 ``.json.tmp``，再 ``os.replace``。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(f".json.{_pid}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("startup_progress: write failed", exc_info=True)
