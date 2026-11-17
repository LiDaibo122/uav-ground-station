# 地图与仿真传感器接入边界

日期：2026-11-13。应用场景：民用自然灾害巡检。

## 当前可以观察到的事实

浏览器使用 CesiumJS 把真实高程和卫星影像渲染为三维场景。地面后端可以接收全球坐标遥测，前端根据遥测显示飞机。Google 3D Tiles 配置入口已实现，但无 API Key，尚未实际加载验证。

这些功能不会自动把场景写入 Gazebo。地图图层无 ROS 订阅话题。`/api/scene/manifest` 返回的是数据源、范围和对齐要求，不是传感器数据。

## 正确的数据链路

```text
地理数据源 ──→ CesiumJS ──→ 地面操作员地图
     │
     └──→ 仿真器场景加载 ──→ 模拟相机 / 雷达 ──→ 观测转换模块
                                   ↑                    │
                         飞机位姿、相机参数、时间          └──→ 地面端

PX4 ↔ 固定翼动力学仿真
  └──→ MAVROS 遥测 ──→ 地面端飞机位置
```

地图相同不代表传感器物理真实性相同。RGB 纹理不能直接充当红外温度、激光反射或雷达回波。灾害外观及标签也需要另建仿真场景。

## 后续对齐契约

| 项目 | 必须明确的内容 |
|---|---|
| 水平坐标 | 地图 WGS84、仿真局部 ENU、PX4 NED 的转换关系与轴方向 |
| 地理原点 | 经度、纬度、参考高度，以及与起飞点的区别 |
| 垂直基准 | DEM 来源高程、WGS84 椭球高、海拔和离地高度分别存储，不能直接混用 |
| 飞机姿态 | 四元数约定、机体系、相机安装外参、云台角度 |
| 时间 | 观测时刻、仿真时钟、接收时刻；仿真暂停不能变成新的传感器采样 |
| 图像 | 图像尺寸、帧率、内参、畸变、编码、来源设备、关联位姿 |
| 场景版本 | 数据源、区域边界、版本、精度与缺失区域 |
| 网络场景 | 地形瓦片尚未加载时的处理，不能误当平地或无障碍 |

## Gazebo 路线

用授权可离线使用的 DEM / 场景模型创建真实几何地形。相机插件应渲染地形及其他模型，输出 ROS 图像或仿真平台支持的视频接口。先用固定相机检查已知地物、方向和高度，再与固定翼位姿关联。纯地形场景也只能验证地形可见性，不能证明建筑碰撞或灾害识别准确。

Google 的在线瓦片不能假定可以导出为本地 Gazebo 世界。需要离线场景时，应另选允许该用途的数据源，或使用自行采集的数据。

## Unreal 路线

Google 官方列出 Cesium for Unreal 为支持的三维渲染方式。使用插件加载地图后，还需独立完成动力学、PX4 仿真传感器接口、相机渲染和图像传输。平台组合、固定翼支持、帧率和地图瓦片加载行为必须实测。

## 当前接口

- `GET /api/scene`：前端默认场景。
- `GET /api/scene/manifest`：可下载的场景对齐说明。
- `GET /api/state?mode=demo|live`：明确分离的演示 / 实际接入数据。
- `POST /api/telemetry`：只读遥测入口；本地 ROS 适配器是其生产者。
- `POST /api/chat`：本地 Qwen 对话，生成草案或澄清问题。
- `POST /api/tasks`：保存草案；返回 `status=draft`、`dispatched=false`。

本版本没有机载命令下发接口、传感器图像入口或物理仿真控制接口。

## 官方参考

- [Google Photorealistic 3D Tiles](https://developers.google.com/maps/documentation/tile/3d-tiles)
- [Google 三维渲染器接入](https://developers.google.com/maps/documentation/tile/use-renderer)
- [Cesium for Unreal：Google 实景三维](https://cesium.com/learn/unreal/unreal-photorealistic-3d-tiles/)
- [PX4 仿真概述](https://docs.px4.io/main/en/simulation/)
- [Terrain Tiles 开放高程](https://registry.opendata.aws/terrain-tiles/)
