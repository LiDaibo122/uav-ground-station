# FieldLink · 山野巡检地面站

面向民用自然灾害巡检的本地地面站原型。默认区域：四川都江堰—龙池山区。

GitHub 建议名称为 `uav-ground-station`。本项目与 XD 底座、跨仓库联调约定分别管理；本机组织及上传步骤见 [仓库整理说明](docs/REPOSITORY_LAYOUT.md)。

当前实现 **Qwen 原生工具调用 → 查询实时状态／规划与比较算法／多轮修改 → 3 架 PX4 固定翼执行 → 遥测反馈**。模型自主选择工具和参数，主对话不再使用用户关键词分流。飞机启动后等待对话任务，原来的六个预设点仅用于回归验证。灾害检测、山区物理场景和相机仿真仍待实现。

默认网页仍显示明确标注的运动演示。交互任务流程见 [Qwen 对话调度说明](docs/QWEN_DISPATCH.md)；原六点验证见 [集群实现与复现](docs/SWARM_SIMULATION.md)。

Windows 已安装 QGC 时，可以用它查看飞行，仅在浏览器保留轻量 Qwen 对话：

```bash
cd /home/ldb/ground_station
GAZEBO_IP=127.0.0.1 python3 -m packages.swarm_sim.demo --hold --tmux --qgc-host auto
```

Windows QGC 使用 UDP14550；Qwen 页面为 **http://127.0.0.1:11843/chat.html**。该入口不打开 Gazebo 图形窗口，后台物理仿真仍运行。网络、机号对应和完整流程见 [Windows QGC 使用说明](docs/QGC_WORKFLOW.md)。

对话现在逐段显示模型正文，并显示推理阶段与工具返回进度；截断时保留已有回执，不自动重发。生成额度、时间预算和验证数据见 [流式对话说明](docs/STREAMING_CHAT.md)。

无论启动窗口是否可见，都可以在普通 WSL 终端停止整套程序：

```bash
/home/ldb/ground_station/stop_all.sh
```

脚本清理本项目的 PX4、Gazebo、专用仿真浏览器、后端、遥测桥、监视进程及带项目标识的 tmux 会话，也停止本机启动脚本配置的 Ollama/Qwen 服务。Windows QGC 和普通浏览器窗口自行关闭。`stop_all.sh --status` 只查看后台状态；`stop_all.sh --keep-model` 停止系统但保留模型服务。可以重复执行，停止记录保存在 `runs/shutdown-latest.json`。如果正在项目 tmux 内，先按 `Ctrl+b`、`d` 脱离，再执行。

```bash
python3 -m packages.swarm_sim.demo --hold
```

仿真页面：**http://127.0.0.1:11843/?mode=live&swarm=1**。上线后在对话中输入“下发巡检任务：东1100米、北50米；东1350米、北400米。由算法选飞机”。按 Ctrl+C 停止该命令创建的仿真。

需要 Gazebo 与终端监视时，使用以下入口，会自动创建并进入 tmux 的 `fieldlink` 会话：

```bash
cd /home/ldb/ground_station
GAZEBO_IP=127.0.0.1 QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 \
  python3 -m packages.swarm_sim.demo --hold --gui
```

`Ctrl+b` 后按 `0–7` 切换运行、总览、状态、位置、电量、任务、Qwen、MAVROS 窗口；`Ctrl+b` 后按 `d` 脱离并继续运行，`tmux attach -t fieldlink` 返回。重复启动会复用此项目会话。`--no-tmux` 保留原启动方式。数据来源与停止方法见 [tmux 监视说明](docs/TMUX_CONSOLE.md)。

## 启动

环境：Node.js >= 18.18（安装 / 打包），Python >= 3.8（HTTP 服务），支持 WebGL 的现代浏览器。仅运行 HTTP 服务无需新增 Python 第三方依赖；集群工具另需 PX4/Gazebo、pymavlink、NumPy 等，详见仿真文档。

```bash
cd /home/ldb/ground_station
npm ci                      # 首次安装；本次开发环境已安装
./start.sh
```

打开 **http://127.0.0.1:11843**。地图图像与高程首次加载需要联网，会逐层细化。

`start.sh` 会在存在 `~/.local/bin/ollama-start` 时启动本机已有 Ollama；不会下载模型。没有 Ollama 时地图仍可使用，聊天显示真实错误。也可直接执行 `python3 server/app.py`。

默认模型是本机已安装的 `qwen3.5:4b`，原有 2B 模型保留，可通过环境变量调整。两者均需检查自然语言理解与反馈，不保证任意指令都正确：

```bash
GROUND_QWEN_MODEL=qwen3.5:2b GROUND_OLLAMA_URL=http://127.0.0.1:11434 ./start.sh
```

服务默认仅监听本机。远程开发环境应转发 11843 端口。不要把开发 HTTP 服务直接暴露到公网。

## 界面

- 真实三维地球、开放高程与卫星影像；拖动、缩放、倾斜、朝北、区域定位、全屏。
- 都江堰—龙池默认区域，A/B 演示巡检多边形，三架固定翼图标与演示环形航迹。
- 设备选择、跟随视角、速度 / 高度 / 电量展示。
- 演示与 PX4 只读遥测模式切换；进入 PX4 模式后立即移除演示飞机。
- Qwen 多轮自然语言对话、算法比较、方案修改与执行、工具调用轨迹和 PX4 回执；草案可单独保存。
- 地图图层开关、Google 3D Tiles 配置、坐标定位、接入状态、场景说明导出。
- 手机窄屏布局；中文字体随工程本地打包。

“暂停演示”冻结显示，恢复后跳到当前演示时刻；不会暂停 PX4 或物理仿真。演示电量是固定样例值。示例航迹不表示已验证地形净空和固定翼可飞性。

## 地图数据

### 默认：卫星影像 + 真实地形

- 影像：[Esri World Imagery](https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer)。本界面保留版权标识；服务可用性、许可与生产使用条件以数据提供方为准。
- 高程：[Terrain Tiles / Mapzen on AWS](https://registry.opendata.aws/terrain-tiles/)。按 Terrarium 格式解码，不是随机地形或示意背景。来源和归属见 [Tilezen attribution](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)。
- 数据通过后端固定白名单瓦片入口转发，解决浏览器跨域与代理差异。仅有容量受限的内存缓存，没有下载完整区域或构建离线地图。
- 卫星影像并非实时灾情；高程表示地表起伏，不包含完整建筑、树木、热辐射、雷达材质等传感器场景。
- 高程源与 MAVROS 的高度基准可能不同。本版将高程用于可视化，没有做生产级大地水准面换算，不能用来判断飞行净空。

### 可选：Google Photorealistic 3D Tiles

点击右上地图来源或设置 → Google 实景三维 → 输入自己的 Map Tiles API Key → 应用。

需要已启用 Map Tiles API 的 Google Cloud 项目和计费配置。Key 只保留在页面内存，不写入服务器、URL、localStorage 或配置文件；Google 瓦片请求仍按 API 要求把 Key 发送给 Google。浏览器 API Key 应按 Google 文档设置来源与 API 限制。

实现使用 `Cesium.createGooglePhotorealistic3DTileset`，保留 Google 与数据提供方版权。缺少 Key 或连接失败时保留原地图，不冒充 Google 图层已经连接。本次没有 Google Key，因此未验证真实 Google 瓦片加载、计费或龙池山区覆盖。

Google 3D 覆盖不是全球每一个地点。可以用定位菜单查看其他地区，但实际巡检区域配置仍是龙池山区。

参考：

- [Google Photorealistic 3D Tiles](https://developers.google.com/maps/documentation/tile/3d-tiles)
- [Google renderer integration](https://developers.google.com/maps/documentation/tile/use-renderer)
- [Google Map Tiles policies](https://developers.google.com/maps/documentation/tile/policies)

## PX4 与传感器的关系

**网页显示地图 ≠ PX4 或 Gazebo 已加载该地图。** PX4 不是三维地图渲染器，相机与雷达的数据由仿真器及其传感器插件产生。

要让无人机相机看到同一地区，有两条后续路线：

1. 保留 Gazebo：使用许可允许的 DEM、纹理或场景模型构建本地地形，并校准地理原点、高程、相机内外参和时间，再由 Gazebo 相机插件发布图像。
2. Cesium for Unreal：在线加载相同地图源，连接固定翼动力学与 PX4 仿真接口，再设置虚拟相机与图像输出。Google 渲染支持不等于已经验证固定翼仿真、碰撞、深度或雷达传感器。

本项目不抓取或转换 Google 三维数据为 Gazebo 离线资产，不把浏览器截图冒充传感器图像。接入页明确显示相机未接入。导出的 JSON 只是场景配置和对齐说明，不是 Gazebo 世界文件。

细节见 [传感器接入说明](docs/MAP_SENSOR_INTEGRATION.md)。

## 只读 MAVROS 遥测

已提供 ROS 适配脚本，此适配脚本尚未与实际 MAVROS 联调。此次固定翼闭环使用独立的直接 MAVLink 仿真适配器，不能代替 ROS 话题联调。ROS 模式下每架机单独启动一个实例，namespace 必须与实际 ROS 话题一致。

```bash
source /opt/ros/noetic/setup.bash
python3 scripts/ros_telemetry_bridge.py \
  --namespace /uav1/mavros --id UAV-01 \
  --ground http://127.0.0.1:11843
```

订阅：`global_position/global`、`vfr_hud`、`battery`、`state`。适配器没有 ROS 发布器，不解锁、不切模式、不发送航点。GPS 过期后停止刷新，地面端按最后接收时间将设备标为过期；未知电量显示“—”。

## 包结构

```text
web/                    前端、Cesium 适配、样式与本地字体
server/app.py           本地 HTTP、Ollama 接入、只读状态与草案记录
config/scene.json       地图区域、数据源、演示区域
packages/swarm_sim/     传感器文本、MAVLink、PX4/Gazebo 仿真和验证
packages/ground_core/   Qwen 结构化任务契约、坐标校验和区域抽样
packages/task_allocation/ 独立任务分配算法，不启动仿真或 ROS
packages/adapters/      XD 适配预留说明；当前网关尚未实现
third_party/            源码版本清单与补丁；检出目录不提交
reports/                本机验证结果与截图，不提交
scripts/                打包与可选 ROS 遥测适配
tests/                  后端边界与浏览器交互验证
docs/                   地图 / 仿真传感器接口说明
runs/                   运行记录、验证报告与截图；不提交版本库
```

前端没有 ROS、CUDA 或 CANN 依赖；ROS 是可选的独立适配进程。未来迁移 310P 时可以保留前端和业务 HTTP 契约，更换模型服务适配。当前 Ollama 运行成功不代表已完成昇腾部署。

首次克隆后，运行仿真或算法测试前先按 [第三方源码说明](third_party/README.md) 恢复固定版本依赖。当前 CBBA 在地面进程内计算，不代表已完成机间分布式部署。

## 验证与打包

```bash
npm test
npm run build
python3 server/app.py --built
```

原有浏览器验证需要运行中的后端、Playwright Chromium 和本地模型，执行前应停止集群仿真，以检查没有遥测时的空状态：

```bash
CHROMIUM_PATH=/path/to/chrome npm run test:browser
```

集群运行时使用 `node tests/swarm-browser.mjs` 检查真实遥测及任务图层；算法检查使用 `python3 -m packages.swarm_sim.verify_auction`。

默认测试浏览器路径使用本开发环境已有的 Chromium。测试会验证真实高程加载、地图交互、演示 / 实际遥测隔离、缺少 Google Key 的错误处理、Qwen 实际生成与草案保存、移动布局。测试草案会在退出时恢复，报告与截图保存到 `runs/`。

生产环境还需要选择正式地图服务方案、接入身份认证与权限管理，以及完成机载协议和仿真联调；当前版本用于本地界面与接口开发。
