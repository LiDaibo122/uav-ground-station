# 仿真启动与 tmux 监视

原启动命令现在会自动创建并进入项目专用 tmux 会话 `fieldlink`：

```bash
cd /home/ldb/ground_station
GAZEBO_IP=127.0.0.1 \
QT_QPA_PLATFORM=xcb \
LIBGL_ALWAYS_SOFTWARE=1 \
python3 -m packages.swarm_sim.demo --hold --gui
```

网页：http://127.0.0.1:11843/?mode=live&swarm=1 。固定翼启动后等待对话任务，不预载巡检点，不自动起飞。

| 按 Ctrl+b，再按 | tmux 窗口 | 显示内容 |
|---|---|---|
| 0 | run | 启动输出、仿真状态变化、进程错误 |
| 1 | overview | 三架机状态、位置、电量和任务总览 |
| 2 | state | connected、armed、mode、landed，区分在地面与离地 |
| 3 | position | 经纬度、原点 ENU、AMSL、相对高度和地速 |
| 4 | battery | 各机电量、电池遥测年龄和低电量提示 |
| 5 | mission | 任务归属、上传回执、模式/解锁 ACK、航点序号、到点和完成状态 |
| 6 | qwen | 最近会话的用户需求、实际工具调用、结果或重复失败原因 |
| 7 | mavros | 真实 ROS Master/MAVROS 是否存在、已发布话题和可读取的 State 消息 |
| 8（可选） | qgc | 使用 `--qgc-host auto` 时添加，显示 Windows QGC 双向转发与机号统计 |

当前执行通信为直接 MAVLink。state 页采用熟悉的 MAVROS 风格字段，但明确标注来源；它不是已接入的 ROS 订阅节点。mavros 页只检查已有 ROS Master 与话题，不为监控而启动第二套 MAVROS，也不争用 SITL UDP 端口。

若在Windows QGC看飞行，可使用 `python3 -m packages.swarm_sim.demo --hold --tmux --qgc-host auto`，浏览器只打开 `/chat.html`；完整说明见[Windows QGC流程](QGC_WORKFLOW.md)。

监视程序通过地面站 HTTP 读取同一份遥测，不额外绑定 MAVLink 端口。逐机标记 LIVE/STALE；HTTP 不可达时可回退现场文件，但显示 FILE/HISTORY、连接 UNKNOWN。历史记录不证明当前仍在飞行。任务 ACK 仅代表接收，到点与完成以真实遥测为准。

## 脱离、返回与停止

- `Ctrl+b` 后按 `d`：脱离 tmux，仿真继续运行。
- `tmux attach -t fieldlink`：重新进入。
- `Ctrl+b` 后按 `0`，再按 `Ctrl+C`：停止 run 窗口中的启动流程，并清理它创建的仿真、后端和 Gazebo；复用的外部服务仍由原启动者管理。其余只读窗口保留，便于查看停止状态。
- `tmux kill-session -t fieldlink`：关闭整个项目会话，并触发其运行进程的清理。它不影响其他 tmux 会话。

重复运行入口会复用本项目的现有会话；若 run 已退出，只重启该窗口，避免重复创建仿真。检测到同名会话属于其他项目时会报错，可改用 `--tmux-session fieldlink-dev`。在其他 tmux 会话里启动时切换到项目会话；非交互终端创建后台会话并打印返回命令。

```bash
# 仅后台启动，不自动进入
python3 -m packages.swarm_sim.demo --hold --gui --tmux-detached

# 保留原来的直接运行方式
python3 -m packages.swarm_sim.demo --hold --gui --no-tmux

# 任意独立终端查看单项快照
python3 -m packages.swarm_sim.monitor --view state --once
python3 -m packages.swarm_sim.monitor --view position --once
python3 -m packages.swarm_sim.monitor --view mission
```

## 模型输出截断与工具重复问题

本次截图文字是“模型输出达到长度上限”，指一次模型生成（含思考）到达输出限额。最新会话 pending 记录为：用户要求执行刚才方案 → execute_plan 返回“已有任务正在执行” → 模型后续生成截断。旧程序直接抛出了长度错误，用户没有看到真正的执行受阻原因。这不等于任务执行了多次，也不等于已经成功下发。

修复后，输出截断仍返回已取得的工具结果和具体错误；有成功回执就保留，有发送后 ACK 不确定的情况则提示以遥测确认，不伪称成功或确定没有发送。真实 pending 夹具与截断测试见 `tests/fixtures/execute-busy-output-limit.json` 和 `tests/test_agent.py`。

同一会话较早还发生了另一种故障：用户请求“起飞3架无人机，分别去执行A/B/S区”。旧接口错误地要求区域任务提供 x/y，模型反复发送缺参请求；补齐后又触发 2.5 km 范围约束，最终耗尽十轮工具调用。三块区域还会展开为九个样点，超过单轮六点上限。

修复后，zone 只需 zone_id，local/wgs84 仍必须提供数值坐标。范围和容量错误返回具体区域、点数及不可原样重试标记；模型循环会停止该轮，不自动扩大范围、删减目标或替换为 S 区。重复错误/无进展提前停止，成功写工具结果缓存防重发，结束前保留总结步骤。单纯提高轮数无法解决这种重复。

区域范围限制没有被移除。调整为仿真范围内的点或 S 区后，可以在同一会话继续规划。

本轮真实 Qwen 复测记录在 `reports/zone-tool-loop-verification.json`，飞控为 dry-run fixture；真实 tmux/三机遥测/退出清理验证在 `reports/tmux-live-verification.json`，这项检查没有发送飞行任务。
