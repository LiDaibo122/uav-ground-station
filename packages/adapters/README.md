# 底座适配预留目录

未来 XD ROS1 适配实现放在此处，统一负责：

- 读取底座状态、定位健康、离地/解锁状态、电量和感知观测。
- 将地面任务转换为 `planning/task_path`，并跟踪对应的 `goal_id`。
- 调用公开的 `control_manager` 起降服务及 `planning/cancel`。
- 管理取消、暂停、恢复和控制权交接。

当前尚未实现 XD 网关，现有工具仍通过 `swarm_sim.Session` 执行。此次仅分离任务分配模块并保留适配位置，不声称底座已接通。

跨仓库接口约定与配置示例集中维护在相邻 `uav-integration/` 仓库中。实现时使用独立 ROS1 进程，使地面核心不直接依赖 ROS Python 环境。
