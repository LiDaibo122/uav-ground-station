# XD 仿真命令发射端

双机联调用 `python3 /home/ldb/uav-integration/scripts/xd_single.py start --dual --web`。工具参数及 ROS 端点可从 `GET /api/xd/interfaces` 读取。按机号分发和隔离草案的代码在 `xd_fleet.py`，模块交接清单和只读自检命令见相邻联调仓库的 `contracts/XD_DUAL.md`。

加载联调工作空间环境后，在地面站仓库用 `python3 -m packages.adapters.xd_command` 发送命令。客户端发布到 `/ground_bridge/command`，按指令编号读取 `/ground_bridge/result`。起飞、航路、释放和返航由 XD 单机桥接包调用底座执行。操作示例在相邻联调仓库的 `docs/XD_SINGLE_RUNBOOK.md`。使用 `python3 /home/ldb/uav-integration/scripts/xd_single.py start --web` 启动单机 Qwen 网页。浏览器访问 <http://127.0.0.1:11844/chat.html>。单机工具与回执处理在 `xd_web.py`，飞行控制由 XD 执行。
