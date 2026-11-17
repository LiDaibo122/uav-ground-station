# 前端延迟排查与轻量对话

2026-11-16 调查区分三类体验：地图渲染负载、遥测采样间隔、Qwen整轮推理等待。它们不能仅凭“页面卡”合并归因。

## 已测得的事实

- 本机主服务11843，8次只读样本：`/api/state?mode=live`中位0.71ms，`/api/simulation`中位1.03ms，`/api/health`中位0.86ms。此时没有证据表明普通本地状态接口需要等数秒。
- 两个实际在线瓦片样本成功返回：高程约1.04秒，影像约0.139秒。只是当时样本，不保证所有瓦片或后续网络稳定。瓦片代理单次超时配置为25秒，网络失败仍会影响地图加载。
- 原地图明确设置`requestRenderMode:false`，并把轨迹作为每帧动态CallbackProperty；静止页面仍持续绘制。任务图元原来已经有missionKey去重，不能说它每两秒无条件重建。
- 原图状态每1秒轮询，任务每2秒轮询；机位视觉上按采样跳动不等于每条MAVLink消息都丢失。Qwen当前是整轮HTTP回复，多个模型/工具步骤会花数秒到数十秒；这段等待与WebGL掉帧是不同问题。

## 本轮改动及验证

原地图改为按需绘制，静态轨迹不再每帧回调；只有新增实际位置时更新轨迹数组。交互期间帧率上限30，关闭不必要的preserveDrawingBuffer。位置更新和相机变动仍能触发出帧。

受控测试强制使用SwiftShader软件渲染、固定平地PNG瓦片、暂停演示遥测：静止3秒由15次渲染降到0次；同区间主线程TaskDuration约0.163秒降到0.075秒。这个测试验证闲置负载，**不是用户Windows浏览器的帧率测量**，也没有验证地图精度。真实地图12秒只读网络探针初始化成功、没有JS异常，未发现本次测试中的长时间API阻塞。

证据：

- `reports/frontend-http-probe.json`
- `reports/frontend-network-probe.json`
- `reports/frontend-performance-before.json`（修改前采集）
- `reports/frontend-performance-after.json`（修改后采集）

## 用QGC看飞行、用轻量页发需求

打开 **http://127.0.0.1:11843/chat.html**。它复用同一Qwen Agent、工具接口和会话，提供自然语言输入、工具进度、实际方案/分配表、回执、token用量及约3秒一次机群快照。未加入关键词口令或替代控制链路。

该页面不加载Cesium、WebGL或在线地图瓦片；关闭原三维页面标签后，飞行态势交给QGC查看。主地图右上也有“轻量对话”入口。QGC接入与启动步骤见 `QGC_WORKFLOW.md`。

基础JS/CSS约15KB。Windows优先使用本机字体；缺少中文系统字体的Linux浏览器会按需加载项目既有约16MB中文OTF兜底，因此不能把该页面全部资源都说成15KB。没有强制预加载中文字体。

轻量页面回放了真实Qwen/算法响应，并显示一次捕获的三机实际状态快照：13项检查通过，包括无地图资源、正确分配与坐标、实际调用轨迹、token计数、桌面/手机无横向溢出及清空对话不影响飞机。该UI测试没有实际调用模型或发飞控指令。证据为 `reports/chat-lite-verification.json` 与 `reports/chat-lite.png`。

```bash
# 只读性能探针；需要本机主服务正在运行
node tests/frontend-network-probe.mjs

# 独立HTTP服务，用于不操作飞机的前端检查
python3 server/app.py --port 11973
# 另开终端，检查当前版本（label不切换源码版本）
node tests/frontend-performance.mjs after
node tests/chat-lite.mjs
```

轻量页会显示等待秒数与工具进度，但不会让模型推理本身变成瞬时，也不会把ACK当作离地、返航或降落完成。
