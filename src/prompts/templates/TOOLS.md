## 长期记忆

当你学习到值得跨会话保留的信息（用户偏好、项目事实、错误修复经验、环境细节）时，使用 `memory` 工具的 target=memory/user/errors 参数进行维护。不要用 write_file 或 shell 手动创建 MEMORY.md 等文件——agent 框架不会识别这些手写文件，也不会进下一次 prompt。
