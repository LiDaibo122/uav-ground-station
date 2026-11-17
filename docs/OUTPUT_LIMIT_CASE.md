# 2026-11-16：生成额度耗尽，不是遥测撑满上下文

用户请求“全部往北飞100m，绕行一周返航”，来源会话5d40ae60-ba62-49c1-8fa2-e39300fa6c22。

| 模型步骤 | 输入token | 生成token | 结束原因 |
|---|---:|---:|---|
| 查询机群状态 | 2360 | 220 | stop |
| 处理工具返回后的下一步 | 3248 | 4096 | length |

输入窗口16384尚有大量空间，模型在第二步用完单次num_predict=4096额度。引擎日志实际n_tokens=7343、truncated=0，不是旧案例16245+139撞满上下文的同一种问题。页面生成4316是两步220+4096的合计，不表示单次超过4096限制。旧记录没有分别保存thinking/content长度，不能断言4096全部用于思考。

底层遥测由Session监视线程写入当前状态；浏览器轮询和状态更新不主动调用Qwen。Agent只有收到/api/chat用户请求才运行；本轮仅调用一次get_fleet_state，取得一次快照，没有连续灌入实时数据。

当前任务工具缺少相对编队位移、有限圈数盘旋以及结束后自动返航的完整序列。原能力说明未充分表达这些缺项。此外，Agent在本轮工具回合间重建assistant消息时丢弃了模型返回的thinking；现按Ollama工具调用约定，仅在当前轮后续模型请求中携带，不在旧历史或前端展示其内容。

处理包括：明确现有能力及缺项；不扩大生成额度、不把未完成输出当作完整指令；区分生成限额和上下文满的提示；记录每步thinking_characters、answer_characters、tool_calls_returned用于后续定位。

原会话和当时飞行快照的真实Qwen隔离回放见 `reports/output-limit-replay.json`：两步生成261+416=677 token，正常结束，未触发飞控fixture动作。模型说明了现有能力限制；这不表示相关复合任务功能已经实现，也不证明解决了所有可能的长推理。

本轮未重新下发用户的飞行指令。代码修复在下次后端启动加载，不为更新对话逻辑打断飞行中的仿真。

后续已增加流式正文、阶段进度与有界等待；真实模型隔离回放两例均正常结束，详情见 [流式对话说明](STREAMING_CHAT.md)。

参考：[Ollama工具调用](https://github.com/ollama/ollama/blob/main/docs/capabilities/tool-calling.mdx)、[思考与工具结果传递](https://github.com/ollama/ollama/blob/main/docs/capabilities/streaming.mdx)。
