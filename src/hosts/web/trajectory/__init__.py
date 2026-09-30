"""轨迹时间线模块（track-timeline-v0.1）。

门户：:class:`hosts.web.trajectory.manager.TrajectoryManager`。
内部文件：``models.py``（投影草稿）、``projector.py``（纯投影器）、
``README.md``（模块文档）。跨模块只 import 本包的 ``TrajectoryManager``。
"""

from hosts.web.trajectory.manager import TrajectoryManager

__all__ = ["TrajectoryManager"]
