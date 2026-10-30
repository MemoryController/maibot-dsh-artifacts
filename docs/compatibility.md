# 官方接口调查与验证边界

正式版本 0.2.1 使用原版 MaiBot 插件能力，不应用 Host 补丁。协议基准为本仓库 `ba8a5fb4dc9857982f9cf5518b746ed0f9c282c8`（0.2.0），本次调查远端 main 仍为该提交，无更新差异。0.2.1 增加请求文件确认入口，manifest v1、现有 Handoff 内核与账本格式不变。固定源码依据：

| 项目 | 提交 |
| --- | --- |
| MaiBot v1.3.18 | [`2a9af62cec37035d29237acbe2cf3adceb700b27`](https://github.com/Mai-with-u/MaiBot/tree/2a9af62cec37035d29237acbe2cf3adceb700b27) |
| SDK v2.19.0 | [`c2a039a7f4bcb367c7d770e88d6e034337be6b12`](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/c2a039a7f4bcb367c7d770e88d6e034337be6b12) |
| 插件中心规范 | [`fa65a39dc9ce941dbb24048aebfcc4d629495f0e`](https://github.com/Mai-with-u/plugin-repo/blob/fa65a39dc9ce941dbb24048aebfcc4d629495f0e/CONTRIBUTING.md) |

manifest v2 仅声明已核对 Host/SDK 版本及实际需要的 files.list/upload/download/send。插件与正式新增模块为 MIT。历史 Host 补丁保留其 NOTICE/GPL 许可记录，不是当前部署方案。未发布正式 Release 或申请中心收录。

## 原版 ACP 及文本交接

[`acp_agent.py:132`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L132) 创建 ACP MCP 服务时只注册 AgentToolProvider、ComputerToolProvider、FileToolProvider，没有第三方 PluginToolProvider。DSH 不能直接调用本插件工具。[`mcp_server.py:44`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/mcp_server.py#L44) 使用独立 Agent 上下文列出/执行工具；内置文件工具也不能据此取得原聊天附件权限。

正式链路：

1. [`provider.py:19`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/provider.py#L19) 的 agent_talk 说明回执仅表示入队。
2. [`messaging.py:70`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/messaging.py#L70) 区分普通聊天/Agent 并校验真实目标，88–89 行把 DSH 回信交给原聊天 enqueue_internal_task。
3. [`runtime.py:726`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L726) 追加 InternalContextMessage 并唤醒 Planner，不解析或执行本项目清单。
4. [`runtime.py:1343`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L1343) 注册 PluginToolProvider；模型实际选工具后才派发插件调用。

CLI/manifest 规范参数，不消除模型选工具步骤；relay 只输出参数，DSH 须实际调用已有 MCP agent_talk。prepared、排队成功、tool_called、上传返回 file_ref、发送 sent 是不同证据。

0.2.1 的受控降级由用户回到原聊天，明确选择 `dsh_import_request(task_id, request_sha256)`。新增 [request.py](../request.py) 只解析固定 `outbox/.dsh-handoffs/<task_id>.json` 的 ≤64 KiB 私有快照，验证准确字节哈希、严格清单、任务标识后，在同次真实调用内复用 `Handoff.import_manifest(..., send=False)`。没有新后台文件权限、可信目标服务或自创 Host 适配器。普通随机 nonce 或请求哈希不能证明原聊天；工具描述及文档要求原聊天明确确认。DSH 不把共享目录内可伪造的 JSON 当官方入库回执。

## 事件与 Hook 为什么不能自动上传

enqueue_internal_task 直接追加上下文和调度，没有插件消息事件派发；不能依赖 ON_MESSAGE 接收传话。[`bot.py:897`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/chat/message_receive/bot.py#L897) 的旧 ON_MESSAGE 调用还是注释，SDK 有枚举不意味着 Host 必发事件。

[`maisaka.planner.before_request`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/chat_loop_service.py#L1254) Hook 可间接看到选中 items、Host 传入的该轮 session_id，但不是专用交接事件。[Hook 派发](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/hook_dispatcher.py#L447)及[事件派发](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/host/event_dispatcher.py#L193)均不签发文件 scope。

Runner 的[事件入口](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L2266)、[Hook 入口](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L2324)也不设置 _file_scope。从回调直接调用被 @Tool 装饰的 Python 方法不等于 Host 派发。保存“最后一次 Hook 的 session_id”会混淆并发聊天，不能作为归属。

SDK [`docs/files.md:15`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/docs/files.md#L15)明确只有 Host 工具派发期间可用文件能力，普通 API、生命周期和后台不可使用。因此本版不提供事件自动入库、后台监听上传或伪装为确定性入口的 QQ 命令。

## 聊天身份与真正的权限边界

[`component_query.py:809`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L809)先复制模型 arguments，828–830 行只补缺失/空上下文值，因此普通 kwargs 的 chat_id/stream_id 可能被模型参数占据，不能作不可伪造的聊天身份证明。本插件不据此建立账本归属。

真正权限来自 [`component_query.py:972`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L972)：Host 按真实执行上下文签发绑定插件/会话的 _file_scope，强制覆盖参数，finally 996 行撤销。Runner [2179 行](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L2179)以调用局部 ContextVar 保存它，[809–810 行](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L809)自动覆盖文件 RPC scope。

SDK [`PluginContext`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/context.py#L162)没有官方 current_chat 身份接口。[ctx.chat 查询](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/chat.py#L57)接受显式目标；ctx.maisaka.context/proactive 同样要求 stream_id，不是当前工具聊天身份凭证。

## 官方文件 API 与缓存授权

| API | 用法 |
| --- | --- |
| [ctx.files.upload](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L66) | 二进制私有快照上传，显式保留原名，不把共享源路径交给 SDK 重开 |
| [ctx.files.download](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L97) | 私有暂存、独占创建及大小/哈希检查，完成后安全发布 inbox |
| [ctx.files.send](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L62) | 当前真实聊天发送；unknown/异常不自动重发 |
| [ctx.files.list(offset)](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L49) | 当前 scope 分页核验全部缓存引用；查询不上传、不发送 |

Host [`files/capability.py:12`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/capability.py#L12)校验插件 scope，从中取真实 sid；21–22 行 list 调用 store.list(sid)，不接受插件选聊天。[`store.py:110`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/store.py#L110)仅列该 sid 的非 deleted FileBinding，默认每页 50 条，普通结果不含 session_ids。[`_authorize():81`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/store.py#L81)拒绝未授权引用。

缓存至少有一个已知 file_ref、全部引用在当前 list 可验证才返回；无引用、缺项/删除、分页未完成或异常时返回中性 unconfirmed，不泄漏路径/元数据。空集不能授权。管理员可把同一 ref 合法绑定多个聊天，所以只能证明当前访问权，不证明原请求聊天身份。已授权缓存也只回当前 Host 元数据和最小登记状态，不含原输入路径/发送记录，delivery 保守为 unconfirmed，不能据缓存恢复副作用或证明原聊天投递。

task_id 只是幂等标识，不是 Host token。账本冻结 manifest/send/工作区配置，副作用前 claim，unknown 不自动恢复。整体数据丢失或备份回退仍破坏去重依据，不承诺 exactly-once。

账本在 `ctx.paths.data_dir/handoff/`，同级独立 `.handoff-initialized` 哨兵为 0600；账本子目录丢失后不能忽略哨兵自动重建，整个 data_dir 丢失/回滚则无法靠目录自身证明旧执行。status/replay 使用只读数据库连接，测试检查磁盘内容及 mtime 不变。

首次处理全部成功为 registered=confirmed，部分成功为 partial；没有成功项且全部在上传前确定失败为 not_registered，存在未知上传结果则为 unknown。缓存缺引用时只能 unconfirmed；发送任务缓存 success=false、delivery=unconfirmed，不借访问权核验复述其他聊天的投递。

## 模型选择、外部依赖与验证

Host [`component_query.py:275`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L275)识别 visible/deferred，默认 deferred。SDK [Tool 装饰器](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/components.py#L415)接收 metadata，官方[测试](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/tests/test_sdk.py#L78)使用 visible。本插件显式 visible，仍可能漏调/错参；实际工具记录和 file_ref 才是证据。

SnowLuma 文件须结构化登记。Host [`integration.py:378`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/integration.py#L378)签注可信驱动来源，[`FileService.ingest():245`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/service.py#L245)要求 FileComponent、file_id、可信来源后才登记。文字文件名不算附件；群上传通知开关见官方[说明](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L9)。本插件不修复这条外部链路。

文件能力默认关闭、HTTP 通道独立于 WebUI及 unknown 语义见官方 [docs/file-service.md](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md)。SDK download 没有客户端 max_bytes 参数，插件导出限额在下载完成后检查；Host 配额和暂存空间仍必要。

本机 Python 3.12 实际运行正式全套 **286 passed，无失败、无跳过**，Ruff 全仓通过：

| 范围 | 数量 | 验证方式 |
| --- | ---: | --- |
| 原文件安全及插件回归 | 103 | 安全模块 57 + 原插件 46，临时文件系统与 SDK mock |
| 纯插件交接账本 | 83 | 严格清单、快照哈希、claim/未知状态、缓存权限、只读重放、独立哨兵等 |
| DSH helper | 48 | 标准库 CLI、清单准备/relay 参数、实际子进程调用，包含不加载 site 的运行检查 |
| 请求文件确认 | 41 | 实际 request/Handoff/SQLite/安全快照，聊天 scope double；严格 JSON、哈希、任务、路径链接、大小、篡改、并发、重启和 unknown 不重传 |
| 新插件/安装接线 | 11 | 装饰器/调用接线及原版 SDK 安装路径，不应用 Host 补丁 |

原版 MaiBot v1.3.18 官方 ManifestValidator 对 0.2.1 manifest 实际校验通过，无 errors/warnings。安装测试实际把五个插件运行文件复制到**不存在任何 Host 源码**的临时 plugins 目录，以真实 SDK 完成 import/create_plugin/on_load，证明普通安装不依赖 Host 改动。所有 ctx.files API 测试使用 mock，不是实际 Host 文件服务、DSH 或 QQ 集成。

本轮没有运行旧 Host 补丁实验，也没有修改官方源码。历史实验测试留在 integrations 下，不混入默认 pytest/CI。CI 配置 Python 3.10/3.12，但本机只执行 3.12；未据配置宣称 3.10 或远端 CI 已通过。

**尚未完成真实 Docker + SSH ACP + DSH + SnowLuma QQ 全链路验收。** 仍需确认镜像/Runner 加载、挂载和 UID/GID、模型选工具、真正 MCP 传话、QQ 入站登记、网络文件通道及平台投递。官方[文件服务说明 38 行](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L38)也保留实机验收要求。部署见 [Docker](docker.md)，可复制 DSH 指引见 [DSH_INSTRUCTIONS](DSH_INSTRUCTIONS.md)。
