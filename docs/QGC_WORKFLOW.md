# Windows QGroundControl 查看固定翼集群

当前推荐组合：**Windows QGC 显示飞行，轻量 Qwen 页面负责自然语言调度，WSL 中的 PX4/Gazebo 提供仿真。** 不需要在 WSL 再启动一份 QGC。

## 完整流程

1. 在 Windows 打开现有 QGroundControl，保持 UDP 自动连接。接收端口为 **14550**。如果没有启用自动连接，在“设置 → 通信连接/Comm Links”添加 UDP 连接，监听端口设为14550并连接。本机已实测 Windows QGC 监听 `0.0.0.0:14550`。
2. 在 WSL 终端启动：

```bash
cd /home/ldb/ground_station
GAZEBO_IP=127.0.0.1 \
python3 -m packages.swarm_sim.demo --hold --tmux --qgc-host auto
```

不加 `--gui` 就不打开 Gazebo 图形窗口，但后台 `gzserver` 仍进行物理和传感器仿真。若需要同时观察 Gazebo，可以加回 `--gui`；QGC 不负责飞机动力学。

3. 等待 tmux 显示固定翼上线；QGC 中会发现三个系统。QGC 的机号与地面站业务编号对应如下：

| QGC 系统号 | 地面站飞机 |
|---|---|
| Vehicle 2 / 系统2 | FW-01 |
| Vehicle 3 / 系统3 | FW-02 |
| Vehicle 4 / 系统4 | FW-03 |

在 QGC 的载具选择器切换当前关注的飞机，查看其位置、模式、高度、速度、电量及 MAVLink 消息。多机的共享地图和当前选中飞机的仪表含义不同；切换选中飞机不会把地面端任务改派给它。

4. 用 Windows 浏览器打开轻量调度页面：

**http://127.0.0.1:11843/chat.html**

该页面不加载 Cesium、三维地图或在线地形瓦片。原三维页面仍可按需打开，但不必为任务对话一直保持运行。

5. 在 Qwen 页面先发送：

```text
先帮我规划，不要执行。
以仿真原点为基准：
巡检点1：东1100米、北50米，常规优先级。
巡检点2：东1350米、北400米，高优先级。
巡检点3：东1650米、北750米，常规优先级。
根据实时位置、电量和可用状态，用CBBA分配飞机，不指定具体飞机。
```

收到方案后再发送：

```text
就按刚才这个方案执行。
```

地面内核完成最新状态校验、分配和任务上传；被分配飞机自动起飞。随后在 QGC 观察实际位置与模式，在轻量页面或 tmux 5号 mission 窗口查看点位到达与完成进度。算法可能只使用部分飞机，未分配飞机保持待命。

若 QGC 的规划页尚未显示 Qwen 上传的新任务，可使用“从载具下载/Download”读取当前选中飞机的任务。该步骤是读取；在 QGC 上传另一套航线会取代这架飞机现有任务。本流程统一由 Qwen/地面内核调度，QGC用来查看飞行。

6. 完成后，如需关闭整个仿真，先 `Ctrl+b` 再按 `d` 脱离，然后在普通终端执行：

```bash
/home/ldb/ground_station/stop_all.sh
```

此脚本清理项目的桥、PX4、Gazebo、后端和 tmux 监视窗口，也清理脱离窗口后的残留进程，并停止本地启动脚本配置的 Ollama/Qwen。需要保留模型时加 `--keep-model`，只查看是否运行时加 `--status`。Windows QGC 是独立应用，关闭其窗口即可。只按 `Ctrl+b`、`d` 不会停止仿真。`--hold` 也表示巡检结束后继续运行。停止结果见 `runs/shutdown-latest.json`。

## 网络链路

| 用途 | 链路 |
|---|---|
| 地面内核执行任务 | PX4 API本地14581/14582/14583 → 内核127.0.0.1:14541/14542/14543 |
| QGC 独立GCS遥测 | PX4本地18571/18572/18573 → 桥127.0.0.1:14550 |
| Windows QGC | 桥独立uplink随机端口 → Windows宿主:14550；回包按系统号返回对应PX4 |

14541–14543 仍由地面内核独占，QGC不绑定这些端口。桥逐帧保留 MAVLink 原始字节与签名，不修改消息内容。带 `target_system` 的消息只返回目标飞机；心跳等无目标/目标0消息广播至三个独立GCS端点。未知系统目标丢弃，不把单机指令误广播到全机群。

QGC 不是“完全只读”的网络客户端：正常连接也会发送心跳、参数读取和能力查询等消息。桥自身不生成任何 MAVLink 消息；操作者在 QGC 中主动执行的控制操作仍会经桥到达相应飞机。

## NAT 地址与连接检查

本机 `wslinfo --networking-mode` 已确认是 `nat`。`--qgc-host auto` 从默认路由获取 Windows 宿主地址，而不使用 `resolv.conf` 的 DNS 地址。2026-11-16 实测宿主为172.24.160.1，WSL为172.24.168.204；重启WSL后地址可能改变，所以默认使用auto。

```bash
wslinfo --networking-mode
ip -4 route show default
```

如果明确知道 Windows 地址，也可指定：

```bash
python3 -m packages.swarm_sim.demo --hold --tmux --qgc-host 172.24.160.1
```

tmux 的8号 `qgc` 窗口显示机号、PX4帧数和Windows回包数；`runs/qgc-bridge-latest.json` 保存最新统计。看到2/3/4心跳且QGC回包计数增长，说明双向链路已建立。只有PX4帧增长、QGC回包一直为0时，检查Windows QGC是否打开、UDP14550是否监听及Windows应用防火墙许可；本机本次测试已收到回包，没有修改防火墙。

手工诊断桥的命令如下；使用前先停止8号窗口中的桥，避免重复监听14550：

```bash
python3 -m packages.swarm_sim.qgc_bridge --windows-host auto --duration 12 \
  --report reports/qgc-bridge-verification.json
```

镜像网络模式与NAT不同，auto会明确拒绝自行猜测或创建localhost自指桥；可先单独验证跨Windows/WSL的localhost接入，或显式配置已验证的Windows可达地址。本次验证只覆盖当前NAT环境。

## 本轮验证与边界

Windows已有QGC进程及14550监听已确认；未启动WSL QGC进行接入验证。真实桥测试收到三个固定翼系统2/3/4、约1.1万PX4帧和79个Windows返回帧，且三机均未解锁。记录见 `reports/qgc-bridge-verification.json`；8项独立测试覆盖MAVLink1/2原字节、带签名帧、多目标混合数据报、目标路由和无效包处理。

QGC提供飞行观测，不会修复已有任务内容。原型当前返航仍可能走原任务中东4公里的备选降落场；它不等于回起飞原点。这与QGC接入无关，详见 `reports/return-landing-verification.json`。

参考：[PX4 v1.13多机仿真](https://docs.px4.io/v1.13/en/simulation/multi_vehicle_simulation_gazebo)、[WSL网络说明](https://learn.microsoft.com/windows/wsl/networking)。
