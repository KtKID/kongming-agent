"""工作流策略子包。

本脚本标记 strategies 子包，并集中说明该子包承载策略协议、策略说明、策略注册管理器和具体策略实现。
作用是把策略控制面与 workflow 生命周期 facade 分离，后续 parallel、map_reduce、pipeline 等策略都在这里扩展。
关键执行流程：manager 维护策略注册表，description/base 定义策略契约，具体策略接收 context 和 payload 后执行编排。
关键函数：本脚本只提供包标记和导出边界，无独立函数。
"""

from __future__ import annotations

__all__: list[str] = []
