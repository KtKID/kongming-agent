/**
 * track-timeline-v0.1 · 轨迹时间线模块入口。
 *
 * 门户：TrajectoryManager（拉取 + 落 store）。组件经 store hooks 消费；
 * wire 类型只从 `@/protocol` 引用（约束 17 / 19）。
 */

export { trajectoryManager, TrajectoryManager } from "./TrajectoryManager";
export { useTrajectoryStore } from "./TrajectoryStore";
export type { TrajectoryLoadError } from "./TrajectoryStore";
export { TrajectoryPanel } from "./TrajectoryPanel";
