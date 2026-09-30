# trajectory · Web 轨迹时间线前端模块（track-timeline-v0.1）

WorkspaceDock「轨迹」页签的完整前端实现：基于
`GET /api/threads/{thread_id}/trajectory`（task-trajectory-backend 交付）
渲染总览统计 + 运行分段 + 时间线总览 + 搜索 + 账本 + 详情面板。

## 门户

`TrajectoryManager`（`TrajectoryManager.ts`）——模块唯一取数入口：
`load(threadId)` 拉取端点并落 store（404 → `not_found`，其它错误 →
`network`）。组件不直接 import `@/lib/api`。

## 文件职责

| 文件 | 职责 |
|---|---|
| `TrajectoryManager.ts` | 门户：拉取 + 错误分类落 store；v0.1 手动刷新（无实时推送） |
| `TrajectoryStore.ts` | zustand per-thread 状态：数据 / 加载 / 错误 / 选中 seq / 搜索词 / 轮次折叠 / 工具调用收起。选中与折叠独立于搜索过滤存在（清空搜索原样恢复） |
| `derive.ts` | 纯派生函数：`filterRecords` / `buildTurnGroups` / `buildLedgerRows` / `laneForKind` / 时长与 token 格式化 |
| `TrajectoryPanel.tsx` | 根组件：统计条（含损坏不完整提示 + 刷新）+ 运行分段 + 时间线 + 搜索框 + 账本 + Inspector；加载 / 空态 / 404 错误态分支 |
| `TrajectoryLedger.tsx` | 账本列表：轮次分组头（折叠；run 0 显示「会话信息」）+ 记录行（#seq 或 sys/usr 标记 / 角色配色 / 摘要高亮 / 耗时 / token / 错误标红）+ 损坏占位；固定行高滚动窗口虚拟渲染 |
| `TrajectoryTimeline.tsx` | 时间线总览：时长视角（条长 ∝ 耗时 / 全局最大耗时），三泳道（输入 / 模型 / 工具），轮次分界线，点选联动 |
| `TrajectoryInspector.tsx` | 底部固定详情面板：工具（入参 / 输出 / 错误）、模型（回复 / 思考 / token / 首字耗时）、用户输入全文；超长默认截断 2000 字符 + 展开全部 |

## 约定

- wire 类型只从 `@/protocol` 引用（约束 17 / 19）；不在本模块重定义 DTO。
- zustand v5 selector 不允许每次返回新引用（`?? []` 陷阱）——用模块级
  `EMPTY_COLLAPSED_KEYS` 常量。
- Inspector 固定在列表外（底部），保证虚拟滚动行高恒定。
- 待做视角（spec feat03「待做」）：序列（等宽）、实际时间（真实时间轴）。
  数据字段（started_at_ms / duration_ms）已齐备，届时在
  `TrajectoryTimeline.tsx` 增加投影函数即可。

## 测试

- `__tests__/derive.test.ts`：过滤 / 分组 / 展平 / 格式化（纯函数）。
- `__tests__/TrajectoryStore.test.ts`：加载三态、选中折叠独立于搜索、reset。
- `__tests__/TrajectoryPanel.test.tsx`：mock `@/lib/api` 走真实 Manager →
  Store → Panel 链路；覆盖加载 / 空态 / 404、统计与分段、损坏占位、
  轮次与调用折叠、时间线点选联动、工具与模型详情、超长截断、搜索
  无命中与清空恢复、手动刷新。
- 跨端 smoke：`tests/unit/test_web_trajectory_smoke.py`（后端产出结构与
  前端 fixture 消费字段对齐）。
