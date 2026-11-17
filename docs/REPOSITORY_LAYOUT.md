# 仓库结构与 XD 联调准备

2026-11-17 整理为三个相邻、独立的仓库：

```text
/home/ldb/
  xd-uav-system/       上游底座，保留历史；建议 GitHub Fork
  ground_station/     当前仓库，建议远程名 uav-ground-station
  uav-integration/    接口约定、配置、GitHub 操作和版本组合
```

当前仓库职责：

| 位置 | 内容 |
|---|---|
| `web/` | 地图和轻量对话界面 |
| `server/` | HTTP、流式响应和状态接入 |
| `packages/ground_core/` | Qwen 工具调用、上下文与任务校验 |
| `packages/task_allocation/` | CBBA、拍卖、匈牙利和贪心分配 |
| `packages/swarm_sim/` | 现有 PX4/Gazebo 执行、遥测、监视和验证 |
| `packages/adapters/` | XD 适配位置说明，尚未实现 |
| `config/` | 当前地面站场景和机群配置 |
| `third_party/SOURCES.json`、`patches/` | 外部算法的确定版本与本地补丁 |
| `tests/fixtures/` | 自动回归必需的小型输入样例 |
| `docs/` | 架构、流程、故障说明 |

`swarm_sim/auction.py` 与 `algorithms.py` 保留旧导入，现有启动和测试入口继续可用。
本次没有把执行器直接换成 XD，也没有改动底座控制代码。

`runs/`、`reports/`、`dist/`、`node_modules/` 和第三方完整检出保留在本机，通过 .gitignore 排除。
地面代码当前仍有仿真固定原点/容量等约束，联调前应由网关配置替代，不能认为目录整理已经完成运行时解耦。

跨仓库接口草案位于相邻仓库 `uav-integration/contracts/GROUND_XD.md`。GitHub 创建、首次提交和 remote 设置的完整操作位于 `uav-integration/docs/GITHUB_SETUP.md`。
