# 原版 MaiBot 1.3.18：减少交接复制操作的调查

**结论：可以让原聊天 Planner 根据 `agent_talk` 返回的结构化参数调用已有导入工具，减少用户复制操作；不能在这些固定版本中保证自动调用，或实现由插件自行派发 Agent 任务、等待回调、确定性地导入原聊天的完整编排。** 本轮没有引入 Host 补丁、私有 API Hook、后台上传或新的服务。

允许用户在发起任务时说明“完成后在本聊天导入，不发送”，属于操作策略；SDK 没有可核验的人类批准信号。此文字、模型参数、任务 ID 和清单哈希都不产生文件权限，也不建立 Host 验证的原任务绑定。实际上传仍必须由当前真实聊天的 Host 工具派发生效。

## 固定调查版本

| 组件 | 基准 |
| --- | --- |
| MaiBot | 原版 v1.3.18，`2a9af62cec37035d29237acbe2cf3adceb700b27` |
| Plugin SDK | v2.19.0，`c2a039a7f4bcb367c7d770e88d6e034337be6b12` |
| 本插件 | main v0.2.1，`8018552bc82ee5fada38f6792a1721410adf5bc8` |
| DSH 配套插件包 | v0.2.1，`f008cdc039122d360dee67b86e1e2004083ba019` |

DSH 插件包版本不等于 DSH runtime 版本；该包声明 Linux、Node ≥24、DSH runtime/peers `0.2.2-alpha.2`。共享 manifest v1 及准确原字节 request 协议保持不变，不为自动化另造任务授权令牌。历史实验目录不属于本方案。

## 官方接口逐项核对

| 问题 | 实际能力 | 边界与源码 |
| --- | --- | --- |
| 原聊天发起 Agent 任务时，Host 是否知道来源？ | 知道。AgentToolProvider 使用真实 ToolExecutionContext；`messaging.tell` 验证 caller 是现有聊天，并在队列记录 reply_to。 | [provider.py:57](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/provider.py#L57)、[messaging.py:61](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/messaging.py#L61)、[acp_agent.py:81](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L81)。这些内部事实没有通过官方插件任务订阅接口提供给本插件。 |
| DSH 可否直接调用本插件？ | 不可以。ACP MCP 只安装 Agent、Computer、File 三个内置 Provider。 | [acp_agent.py:132](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L132)。MCP [执行上下文](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/mcp_server.py#L44)固定为独立 Agent，会话不是发起任务的 QQ 聊天。 |
| DSH 回信会触发插件消息事件吗？ | `agent_talk` 会向目标真实聊天追加内部上下文并唤醒 Planner。 | [messaging.py:85](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/messaging.py#L85)、[runtime.py:726](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L726)。此路径没有插件消息事件或任务完成回调；不能把 SDK 的 ON_MESSAGE 枚举当作必发通知。 |
| 插件可否在事件/Hook 里上传？ | 不可以。事件和 Hook 派发不签发文件 scope。 | [event_dispatcher.py:193](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/event_dispatcher.py#L193)、[hook_dispatcher.py:447](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/hook_dispatcher.py#L447)、[SDK 文件约束](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/docs/files.md#L15)。直接调用装饰过的 Python 方法也不会获得 Host 权限。 |
| 插件工具可否通过 SDK 调用 `agent_talk` 并等待？ | SDK 没有该能力。`ctx.tool` 只有工具定义内省，`ctx.api` 只转发插件 API。 | [ToolCapability](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/tool.py#L14)、[APICapability](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/api.py#L27)。`ctx.call_host_method` 还有[明确白名单](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/context.py#L132)，不能借它执行任意 Host 工具。 |
| 插件是否有不可伪造的当前聊天 ID？ | `ctx.files` 的权限由 Host 绑定；普通参数不能当作当前聊天凭证。 | [component_query.py:809](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L809)先复制模型参数，再仅补缺失/空的 chat_id/stream_id；[972 行](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L972)才强制注入真实文件 scope。 |
| Planner 可否在回信后调用导入工具？ | 可以。原聊天注册 PluginToolProvider；现有结构化参数已足够调用。 | [runtime.py:1331](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L1331)。工具选择和参数转录仍由模型决定，入队或模型答应不算调用证据。 |
| 能否用主动聊天 API 补齐？ | 能追加上下文/触发 Planner，不能以当前工具身份调用 Agent 或确定性调用导入。 | SDK [MaisakaProactiveCapability](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/maisaka.py#L119)要求显式 stream_id；[runtime.py:673](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L673)仍把任务交给 Planner。没有因此取得原任务授权。 |

### 为什么不通过 Planner Hook 强制插入工具调用

官方 `maisaka.planner.before_request` 可以修改上下文和工具定义，`after_response` 可以修改模型输出。后者确实可能插入 function call，再让正常 Host 工具派发赋予真实聊天 scope；不能简单以“Hook 自身没有 scope”就断言所有间接方案不可能。

本场景仍缺少可信触发条件：

1. [before_request 调用](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/chat_loop_service.py#L1254)只有选中的序列化 Context Items、统计和当前 session_id，没有 Host 验证的 Agent 任务/relay 来源。
2. [ContentContextMessage.to_context_item](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/context/messages.py#L603)转换时不携带 source_kind/message_id；InternalContextMessage 同样投影为 user 角色。[ContextItemMeta](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/llm_models/payload_content/context_item.py#L45)只有 item_id、logical_turn_id 和 timestamp。用户模仿传话文本与真正 Agent 传话不能靠这些字段可靠区分。
3. [after_response 调用](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/chat_loop_service.py#L1360)也没有新增任务来源信息。前后 Hook 不共享可验证的 Agent 任务授权；按“最后一次 session_id”保存状态还会混淆并发回合。

因此不解析 `[传话…回复目标…]`、`source_kind` 文字、task_id、请求文件出现或模型的“已批准”来强制插入导入调用。真实文件 scope 只能保证该次实际调用发生在当前聊天，不能证明这份首次清单属于此前某个 Agent 任务。放宽成任意消息触发，或扫描共享目录后选择一个聊天，超出了此项目的权限约束。

## 一次性长工具等待为何未实现

在一次尚未结束的真实工具调用中，短时间异步等待并使用 `ctx.files` 本身不违反 SDK；本项目不把“所有等待都不合法”作为结论。缺口是：没有官方派发 Agent、取得绑定任务句柄、等待可信结果的组合接口。只增加 `dsh_wait` 不能补齐它。

还必须处理以下实际机制，不能承诺调大超时就能工作：

| 风险 | 固定版本中的事实与要求 |
| --- | --- |
| Host 超时和 scope 到期 | 组件 RPC [默认 60 秒](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/component_timeout.py#L6)；scope [最长签发 3600 秒且 finally 撤销](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L970)。组件可声明 timeout_ms，但不能延长已签发权限或在工具返回后使用。 |
| 取消不是已撤回副作用 | [Host RPC 等待超时](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/rpc_server.py#L228)仅移除本地 pending Future；[Runner 接收处理](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/rpc_client.py#L307)直接 await handler，没有与之配套的远程取消确认。Host 撤销 scope 后，Runner 计算仍可能结束；不能把超时当作“肯定没上传”。 |
| 顺序执行与死锁 | Planner [逐项 await 工具](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/reasoning_engine.py#L2066)。若先执行等待工具、后执行 agent_talk，后者不会被派发；若等待同一 Planner 消费回信后再回调，Planner 又正在等待该工具，形成循环依赖。 |
| 并发与资源占用 | 多聊天等待会同时占用 RPC、scope 和文件资源；必须有有界并发/批量/大小/总时限，且不能用一个全局“最近聊天”映射。单纯轮询文件名无法证明任务归属。 |
| 重启与回执丢失 | scope 和 RPC 不可持久化；重启后必须由 Host 新建合法调用，而非恢复旧 ContextVar/token。已有账本 claim、unknown 状态和禁止盲重传继续适用；备份回滚/完整数据丢失不保证 exactly-once。 |

`ctx.files` [Runner 自动注入](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L809)的调用局部 scope 不读出、不保存、不转交 DSH。异步工作不能留到原工具返回之后再上传。

## 当前可用的最短路径

用户在原 QQ 聊天发起一次任务，并明确希望产物回到本聊天、仅导入。Planner 用正常 `agent_talk` 下发；DSH 生成 outbox 文件，调用现有 prepare/publish，再经已有 `agent_talk` 返回严格 manifest 或 request 确认参数。原聊天 Planner 若实际选择导入工具，插件就在该次合法 scope 内校验并上传。后续是否发送必须另有明确请求。

DSH 包 [relayText](https://github.com/MemoryController/dsh-maibot-artifacts/blob/f008cdc039122d360dee67b86e1e2004083ba019/src/core.ts#L70)已经要求原聊天实际调用 `dsh_import_handoff` 并报告 file_ref，但默认 [publish](https://github.com/MemoryController/dsh-maibot-artifacts/blob/f008cdc039122d360dee67b86e1e2004083ba019/src/core.ts#L288)没有可信目标时仅发布共享请求，**不会自动通知**。不能把 `result=submitted` 当作回信已经排队。

DSH 的可选 [TrustedTargetResolver](https://github.com/MemoryController/dsh-maibot-artifacts/blob/f008cdc039122d360dee67b86e1e2004083ba019/src/index.ts#L10)是宿主提供的接口定义；原版 MaiBot ACP 不提供对应的结构化任务绑定注入。没有部署、实测这个可信集成，就不能宣称 `maibot_artifact_publish` 已自动选中原聊天。人工核实已有 MCP agent_talk 目标后由 DSH 发送结构化参数，可避免用户把确认参数再复制到 QQ；这仍有 Agent 路由和 Planner 决策边界，不是 Host 验证的确定性编排。

必须分别记录这些证据：DSH prepare 成功；共享请求发布成功；agent_talk 入队；Host 真正派发导入工具；upload 返回 file_ref；同聊天 files_list 可查询；发送取得 sent 回执。缺少任何后续证据都停留在未确认状态，不能自动更换 task_id 重新执行。首次任务来源未知的问题仍存在；账本只在当前聊天有权查询所有已登记引用时才返回缓存，不把 task_id 作为授权。

## 最小上游能力建议（未实现的接口契约）

以下是供 MaiBot 官方讨论的扩展需求，不是 v1.3.18 已存在的 API，也不是要求用户自行打补丁：

1. **当前调用绑定的 Agent 任务派发能力**：Host 从真实 ToolExecutionContext 取得 origin，而不是接受插件/模型的聊天 ID；派发后返回不透明任务句柄，绑定插件、原聊天、Agent 会话、任务和期限。DSH 只接收该任务的受限交接句柄。
2. **任务结果到新工具调用的受控恢复**：Agent 结果回调由 Host 验证句柄并只派发已声明的插件交接工具；为该次调用重新签发短期文件 scope。插件不持久化旧 scope，不在普通事件/后台获得任意聊天访问权。超时后能以同一幂等键查询任务/登记事实，而非重新上传。
3. **可验证来源与生命周期**：如果采用事件方案，应携带 Host 验证的任务来源类型、关联句柄和原任务授权状态；明确取消确认、到期、并发上限、进程重启恢复及回执未知状态。只增加 session_id、普通 ON_MESSAGE 或可改写提示词仍然不足。

单纯把 PluginToolProvider 加到 ACP MCP 并不能满足以上要求：它仍在独立 Agent 上下文执行，不会自动还原原聊天和任务授权。

## 验证范围

本调查阅读的是固定官方源码与 SDK；协议测试可执行真实 DSH 编译产物、插件安全快照及账本，但使用模拟 Host Files API 的引用仍只是测试数据。临时安装测试不修改 Host。没有访问生产配置或凭据，没有启动真实 Docker/QQ 聊天、SSH ACP/DSH 会话或 SnowLuma。

真实联合验收仍需在测试部署中观察 Host 工具调用记录、upload 返回的实际 file_ref、同聊天 files_list，以及后续图片工具使用的实际输入引用。文件库里的 image/png 附件不会仅因登记就自动变成聊天媒体消息；应核对后续图片工具到底接受 file_ref、导出的路径，还是聊天图片引用，不能把 MIME 或模拟解码成功描述为真实图片工具集成通过。不同聊天读取权限始终由 Host 文件服务校验；原聊天发送是否成功始终以平台回执为准。
