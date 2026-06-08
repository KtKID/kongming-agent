"""kongming-agent web 宿主壳（v0.1.5+）。

通过 FastAPI + WebSocket 在浏览器侧承载 multi-thread 对话能力。本包内子模块：

- :mod:`web.protocol`：WS / REST 协议层（Pydantic + 字段 contract，被前端 TS 复刻）

后续 v0.1.5 任务（thread-manager / web-host-adapter / web-app-shell）会向本包追加
更多子模块。当前 v0.1.5-A 阶段只交付协议层。
"""
