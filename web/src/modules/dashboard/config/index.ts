/**
 * dashboard/config 子模块对外唯一入口。
 *
 * 仅导出 ConfigPage（路由层挂载）。其他符号（store / api / 组件等）均为内部实现，
 * 禁止 sibling 模块跨子目录 import。
 */
export { ConfigPage } from "./ConfigPage";
