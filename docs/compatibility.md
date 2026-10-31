# 官方接口调查与验证边界

本轮 0.3.0 候选版使用原版 MaiBot 插件能力，不应用 Host 补丁，基于已合并 PR #3 的 main [`8365fcedc93fcb6dcc2bc9f92ed7a65e53d474d2`](https://github.com/MemoryController/maibot-dsh-artifacts/tree/8365fcedc93fcb6dcc2bc9f92ed7a65e53d474d2)（0.2.1）。增加按内容识别图片 MIME、Pillow 依赖与 `dsh_open_image`，不改变 manifest v1、账本或文件权限。历史 0.2.0 基准为 [`ba8a5fb4dc9857982f9cf5518b746ed0f9c282c8`](https://github.com/MemoryController/maibot-dsh-artifacts/tree/ba8a5fb4dc9857982f9cf5518b746ed0f9c282c8)。固定官方源码依据：

| 项目 | 提交 |
| --- | --- |
| MaiBot v1.3.18 | [`2a9af62cec37035d29237acbe2cf3adceb700b27`](https://github.com/Mai-with-u/MaiBot/tree/2a9af62cec37035d29237acbe2cf3adceb700b27) |
| SDK v2.19.0 | [`c2a039a7f4bcb367c7d770e88d6e034337be6b12`](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/c2a039a7f4bcb367c7d770e88d6e034337be6b12) |
| 插件中心规范 | [`fa65a39dc9ce941dbb24048aebfcc4d629495f0e`](https://github.com/Mai-with-u/plugin-repo/blob/fa65a39dc9ce941dbb24048aebfcc4d629495f0e/CONTRIBUTING.md) |

manifest v2 仅声明已核对 Host/SDK 版本及实际需要的 files.list/upload/download/send。插件与正式新增模块为 MIT。历史 Host 补丁保留其 NOTICE/GPL 许可记录，不是当前部署方案。未发布正式 Release 或申请中心收录。

## DSH 版本事实与固定组合

只读核对 DSH 仓库 package.json、lockfile、源码和远端 refs：

- 历史插件包 **0.2.0**：[`0958d8ac788ebbea5f2c507f3b024de3dd06cb24`](https://github.com/MemoryController/dsh-maibot-artifacts/tree/0958d8ac788ebbea5f2c507f3b024de3dd06cb24)。
- 当前 main 插件包 **0.2.1**：[`f008cdc039122d360dee67b86e1e2004083ba019`](https://github.com/MemoryController/dsh-maibot-artifacts/tree/f008cdc039122d360dee67b86e1e2004083ba019)，已合并提交恢复修复。上游部分文档仍称它是“候选、尚未合并”，属于陈旧文档，不改变实际 main/包版本。
- 核查时远端没有 tags；没有据包版本或 main 分支宣称正式 Release。两版都声明 Linux、Node ≥24、DSH runtime / 工具 peers **0.2.2-alpha.2**。插件 0.2.0 与 DSH CLI 0.2.0 不能混为一谈，不据此前者声明后者兼容，也不替用户重装运行时。

| 固定 DSH 插件与模式 | 固定 MaiBot 插件 | 协议结论 |
| --- | --- | --- |
| 0.2.1 main f008cdc…，默认 manifest | 当前 main 0.2.1 8018552… | dsh_import_handoff，完整参数由原聊天确认或按已有任务授权接力 |
| 0.2.1 main f008cdc…，显式 request | 当前 main 0.2.1 8018552… | 固定请求路径/字节 SHA/清单兼容 |
| 0.2.1 main f008cdc…，默认 manifest | 历史 0.2.0 ba8a5fb… | 兼容已有 dsh_import_handoff，无请求工具 |
| 0.2.1 main f008cdc…，request | 历史 0.2.0 ba8a5fb… | 不支持，该工具不存在；使用默认 manifest |
| 历史 0.2.0 0958d8ac…，默认 request | 当前 main 0.2.1 | 请求协议兼容，但旧 DSH 发布后记录失败的状态缺陷不因对端升级而修复，不推荐回滚使用 |
| 历史 0.2.0 0958d8ac…，默认 request | 历史 0.2.0 ba8a5fb… | 默认交互不兼容；只能人工把完整 manifest 交给已有 dsh_import_handoff |

[PR #2](https://github.com/MemoryController/maibot-dsh-artifacts/pull/2) 已合并为上述 main；其[审阅记录](pr-2-review.md)保留当时事实。本轮[自动化调查](automation-research.md)与[可选流程](automation.md)以当前 main 为基准，未创建 Release。

0.3.0 保留上述 0.2.1 的共享交接协议兼容性；新增 `dsh_open_image` 只存在于 0.3.0，不要求升级 DSH 配套包。Pillow ≥12.3、<14 是新增 Runner 依赖，Python 3.10 由 pip 选择兼容 12.x。图片 MIME 只用于完整验证的静态 PNG/JPEG/WebP，图片上下文在原聊天后续工具调用中按需返回。历史 octet-stream 图片不需要重传或改登记信息；具体链路和下游图片工具条件见[图片上下文](images.md)。

用户指定 `WhiteCloudOL/maimai-drawpic-plugin v2.1.3`（`56284ac…`）**不属于已支持的图生图组合**：其 schema 没有工具媒体输入，SDK 声明上限为 2.17.0，错误把索引填成 source_message_id 还可能回退到近期其他图片。源码及独立 2 项负向检查见[图片文档](images.md)，未修改第三方插件。Host 支持工具媒体解析不等于该图片插件支持。

### 精确字节协议与状态

两端均使用 `outbox/.dsh-handoffs/<32位小写hex>.json`，内容是原始 manifest v1，只有 type/version/task_id/files，文件项只有 path/size/sha256。DSH [shared-request.ts](https://github.com/MemoryController/dsh-maibot-artifacts/blob/f008cdc039122d360dee67b86e1e2004083ba019/src/shared-request.ts#L57) 用 `Buffer.from(JSON.stringify(manifest))` 得到 UTF-8 原字节，不追加换行，以此计算 SHA-256。MaiBot 先验证 ≤64 KiB 安全快照的原字节哈希，再解析严格 UTF-8 JSON，拒绝重复键、NaN/Infinity、额外字段和不匹配任务。重新排版、加换行或改编码后不能沿用旧哈希。

manifest 的路径是 outbox 相对路径、size 为非负整数、哈希为 64 位小写 hex，不包括 chat/session/target/send。DSH 正常生成的中文/二进制条目属于本插件可接受集合；默认两端 50 MiB/8 文件、配置硬上限 1 GiB/32，仍须满足两端实际较小限制和 Host 配额。

DSH 本地 `state` 仅 prepared/queued/unknown，成功发布另用 `result="submitted"`，不要等待不存在的 state=submitted。默认流程的 submitted 不代表通知原聊天，`maibot_registration` 仍为 unconfirmed；可选可信通知流程 queued 也只证明入队。本地 status 不查 MaiBot 文件库、不根据共享 receipt JSON 升级为 imported。原聊天实际调用工具、官方上传返回每项真实 file_ref，并用同聊天 files_list 核验，才是入库证据。测试 double 的引用仅是测试数据。

用户必须明确授权导入该原任务产物；可在原聊天发起时给出，不必等待产物后再次复制参数。task_id、请求 SHA 和清单仅确认内容，不提供原聊天授权。默认 manifest 模式与显式 request 模式均不能绕过真实 scope、自动选择原聊天、自动发送，unknown 不能自动重试。

这里的“人类明确确认”是操作/模型策略边界，**不是插件代码已证明批准的安全保证**。SDK 2.19.0 没有本插件可验证的 human-approval 事件；工具描述要求确认不等于收到可核验批准。代码不解析历史消息、Agent 文本或 manifest 中的“已批准”来构造授权，只使用真实 Host scope 限定文件操作。人类意图和正确原聊天须在部署流程中核对。

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

本轮 Python 3.12 默认套件实际运行 **430 passed，无失败、无跳过**，Ruff 和 diff 检查通过。此前 PR #2 的 303、PR #3 的 331 是历史基线；当前默认套件分层如下：

| 范围 | 数量 | 验证方式 |
| --- | ---: | --- |
| 原文件安全及插件回归 | 103 | 安全模块 57 + 原插件 46，临时文件系统与 SDK mock |
| 纯插件交接账本 | 83 | 严格清单、快照哈希、claim/未知状态、缓存权限、只读重放、独立哨兵等 |
| DSH helper | 76 | 标准库 CLI、清单准备/relay/relay-request、严格请求与准确原字节 SHA、失败无输出，包含不加载 site 的实际子进程检查 |
| 请求文件确认及新插件/安装接线 | 69 | 实际 request/Handoff/SQLite/安全快照、严格 JSON/哈希/链接/篡改、取消清理、只读重放、部分成功、阶段字段及无 Host 补丁安装 |
| 图片安全与按需打开 | 99 | 真正 PNG/JPEG/WebP 解码、伪后缀/损坏/动画、WebP 原生解码前尺寸校验、字节/像素/边长/帧预算、当前文件授权与取消清理、三个导入入口 MIME |

原版 MaiBot v1.3.18 官方 ManifestValidator 对 0.3.0 manifest 与 Pillow 包依赖实际校验通过，无 errors/warnings。安装测试实际把插件运行文件和 manifest 复制到**不存在任何 Host 源码**的临时 plugins 目录，以真实 SDK 完成 import/create_plugin/on_load，证明普通安装不依赖 Host 改动。默认套件的 ctx.files API 使用 mock，不是实际 Host 文件服务、DSH 或 QQ 集成。

独立跨语言测试 [protocol_checks/dsh_protocol.py](../protocol_checks/dsh_protocol.py) 对真实 DSH dist 执行 ArtifactService 与文件系统操作，再将准确原字节交给真实 Python request/Handoff/SQLite；Files API 是 scope double。固定 DSH 0.2.0 `0958d8ac…`、0.2.1 `f008cdc…` 各 **12 passed，共 24**，不混入默认 430。覆盖中文/emoji、有效 PNG/空文件/二进制、原字节 SHA、默认模式、真实 CLI 参数接力、零自动通知/未调用不入库、重复/重启、跨聊天拒绝、丢失回执不重传和字节篡改。这不是启动真实 DSH/ACP 会话。

另有[隔离官方文件/媒体组件检查](../host_checks/README.md) **13 passed**：实际运行固定 DSH 0.2.1 dist、官方 SDK、回环 HTTP、原版 FileService/FileStore/capability/provider 和临时 SQLite。测试夹具代替 dispatcher 签发/撤销真实 file ticket，临时聊天注册表和 QQ transport 为 doubles；验证官方临时引用与 files_list、SDK 下载、scope 拒绝、重复/重建、HTTP 回执丢失/取消及 unknown 不补发。图片结果经真实 RPC codec、原样抽取的官方转换方法、真实 ImageComponent/ToolResultMediaMessage 和完整官方解析能力处理器验证；包含历史 octet-stream 与视觉上下文投影。它不证明完整 Host 模块、真实聊天派发、目标图片插件、Agent 路由或 QQ 投递。产品代码未增加 HTTP 服务。

复现独立协议检查时，需要 Node ≥24，使用不含本地改动的固定源码目录；脚本只读其 dist，不自行抓代码或重建包。例如在本仓库已有虚拟环境中：

```bash
DSH_ARTIFACTS_SOURCE=/path/to/dsh-0.2.0 \
DSH_ARTIFACTS_COMMIT=0958d8ac788ebbea5f2c507f3b024de3dd06cb24 \
.venv/bin/python -m pytest -q protocol_checks/dsh_protocol.py

DSH_ARTIFACTS_SOURCE=/path/to/dsh-0.2.1 \
DSH_ARTIFACTS_COMMIT=f008cdc039122d360dee67b86e1e2004083ba019 \
.venv/bin/python -m pytest -q protocol_checks/dsh_protocol.py
```

目录 HEAD 必须恰好是对应完整 SHA，package/src/dist 的改动会使检查失败。CI 独立 Node 24/Python 3.12 job 固定相同双提交及官方组件源，默认 Python job 配置 3.10/3.12；远端结论以当前提交实际记录为准。本轮未跑旧 Host 实验，未修改官方源码，也未执行生产 Host/SSH/QQ。历史验证分层见 [PR #2 审阅记录](pr-2-review.md)，当前自动化边界见[源码调查](automation-research.md)。

**尚未完成真实 Docker + SSH ACP + DSH + SnowLuma QQ 全链路验收。** 仍需确认镜像/Runner 加载、挂载和 UID/GID、模型选工具、真正 MCP 传话、QQ 入站登记、网络文件通道及平台投递。官方[文件服务说明 38 行](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L38)也保留实机验收要求。部署见 [Docker](docker.md)，可复制 DSH 指引见 [DSH_INSTRUCTIONS](DSH_INSTRUCTIONS.md)。
