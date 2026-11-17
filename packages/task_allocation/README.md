# 地面任务分配

- `cbba.py`：CBBA 源码适配、任务收益与可分配性约束。
- `solvers.py`：CBBA、基础拍卖、匈牙利与贪心算法统一入口。

输入为标准化飞机状态和任务点，输出为分配结果与估计指标，不启动 PX4、Gazebo 或 ROS。
目前 CBBA 在地面进程内运行同步一致性计算；不是已经部署到多架机载电脑的分布式节点。

`packages/swarm_sim/auction.py` 和 `algorithms.py` 保留兼容导入。仿真启动命令不变。
第三方源码恢复方式见 `third_party/README.md`。
