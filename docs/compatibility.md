# v1.3.18 / SDK v2.19.0 源码核对

本文记录实现依据及验证边界，避免将“具备源码调用链”写成“真实平台已经完成自动化验收”。

## 固定版本

| 项目 | 核对版本/提交 |
| --- | --- |
| MaiBot | v1.3.18：[`2a9af62cec37035d29237acbe2cf3adceb700b27`](https://github.com/Mai-with-u/MaiBot/tree/2a9af62cec37035d29237acbe2cf3adceb700b27) |
| maibot-plugin-sdk | v2.19.0：[`c2a039a7f4bcb367c7d770e88d6e034337be6b12`](https://github.com/Mai-with-u/maibot-plugin-sdk/tree/c2a039a7f4bcb367c7d770e88d6e034337be6b12) |
| 插件中心贡献规范 | [`fa65a39dc9ce941dbb24048aebfcc4d629495f0e`](https://github.com/Mai-with-u/plugin-repo/blob/fa65a39dc9ce941dbb24048aebfcc4d629495f0e/CONTRIBUTING.md) |

manifest v2 的 Host 最小/最大版本均为 `1.3.18`，SDK 最小/最大版本均为 `2.19.0`。SDK 的使用门槛为 2.19.0，范围暂不向未验证版本外推。manifest 声明 `files.upload`、`files.download`、`files.send`，不申请管理员能力。本项目未创建正式 Release，未申请插件中心收录；官方中心要求至少一个非草稿 Release，完成真实部署验收后可另行决定发布。

插件与本仓库新写集成模块使用 MIT；`host.patch` 中 MaiBot 上游上下文及修改保留 GPL-3.0 许可。见[NOTICE](../integrations/maibot_1_3_18/NOTICE.md)和[COPYING.MaiBot](../integrations/maibot_1_3_18/COPYING.MaiBot)。MIT 不重新许可上游，也不改变分发修改后 MaiBot 的 GPL 要求。

## ACP 为什么不能直接调用第三方工具

[`src/agent/acp_agent.py:132`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L132) 创建 `ToolMCPServer` 时，传入的只有：

```python
[AgentToolProvider(self.service),
 ComputerToolProvider(self.service.computer, visible=True),
 FileToolProvider()]
```

这里没有 `PluginToolProvider`。因此，虽然原聊天可以调用本插件，DSH 经原版 ACP 连接获得的 MCP 工具集中不会自动出现 `dsh_import` 或 `dsh_export`。

[`src/agent/mcp_server.py:49`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/mcp_server.py#L49) 的 MCP 列表和调用都使用该 MCP 服务持有的 Agent 会话上下文。其内置 `FileToolProvider` 也不能凭原聊天的 `file_ref` 自动取得原聊天权限。普通插件安装没有往 ACP 服务注册 Provider；仓库另行提供的可选 Host 补丁新增专用 provider，并不暴露第三方插件工具。见[扩展安装说明](../integrations/maibot_1_3_18/README.md)。

## 实际的文本交接链路

1. [`src/agent/provider.py:19`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/provider.py#L19) 声明 `agent_talk(text, target, images)`。工具说明已经指出：回执表示入队并触发 Planner，不代表已经向 IM 发言。
2. [`src/agent/messaging.py:61`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/messaging.py#L61) 区分 Agent 和普通聊天调用。Agent 回信的 `target` 先通过 `resolve_chat()` 校验为真实聊天；88–89 行获取该聊天运行时并调用 `enqueue_internal_task(...)`。普通聊天向 Agent 发话时则把原聊天记录为 `reply_to`。
3. [`src/maisaka/runtime.py:726`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L726) 将传话作为 `InternalContextMessage` 加入原会话，唤醒正常 Planner。它没有识别或自动执行本项目的 JSON 交接格式。
4. [`src/maisaka/runtime.py:1343`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L1343) 在原聊天工具注册表中注册 `PluginToolProvider()`，同时注册官方文件工具。
5. [`src/maisaka/reasoning_engine.py:1477`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/reasoning_engine.py#L1477) 从运行时真实聊天构造 `ToolExecutionContext`；[`src/plugin_runtime/tool_provider.py:47`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/tool_provider.py#L47) 把调用转交 `component_query_service.invoke_tool_as_tool`。

所以不改 Host 时采用 `DSH → agent_talk → 原聊天 Planner → 插件 @Tool → ctx.files`。文本中给出准确路径与建议参数，仍需模型产生实际工具调用，不能以传话 `delivered=true` 代替工具执行成功证据。

## 文件权限与 SDK API

Host 在 [`src/plugin_runtime/component_query.py:972`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L972) 根据当前真实执行上下文给插件签发 `_file_scope`，覆盖模型可能提供的同名参数，finally 中撤销。Runner 在 [`runner_main.py:2179`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/runner/runner_main.py#L2179) 用调用局部上下文保存该值，调用能力时自动附加。

SDK 的 [`docs/files.md:15`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/docs/files.md#L15) 明确限制：只在 Host 派发的工具调用期间可用，普通 API、启动回调和后台任务无此权限。插件不保存 scope，不接受 `session_id`，不在工具返回后继续传输。

| 官方 API / 源码 | 本插件用法及边界 |
| --- | --- |
| [`ctx.files.upload(path, name, mime_type)`，SDK files.py:66](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L66) | 官方 SDK 从本地路径以二进制读取，通过 Host 票据 HTTP 上传；本插件先生成私有快照，显式传原始文件名 |
| [`ctx.files.download(file_ref, destination)`，SDK files.py:97](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L97) | SDK 独占创建目标，校验声明大小和 SHA-256，失败删除部分文件；插件使用私有暂存再发布，保留最终路径不覆盖语义 |
| [`ctx.files.send(file_ref)`，SDK files.py:62](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/capabilities/files.py#L62) | 发送到当前聊天；保留 `sent` / `unknown`，发送异常或未识别状态保守记为 `unknown`，避免已投递后的网络异常导致重发 |
| [`FileStore.path()`，Host store.py:61](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/store.py#L61) | `file_ref` 为 32 位小写十六进制，不是磁盘路径；插件也作格式校验，存在性和权限仍必须由 Host 决定 |
| [`FileStore._authorize()`，Host store.py:81](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/store.py#L81) | 需要 `file_ref` 与当前 `session_id` 的绑定；插件不调用管理员接口，不为其他聊天授权 |

SDK 下载接口没有 `max_bytes` 参数；插件无法在调用前仅靠 `file_ref` 知道大小。导出先由 Host/SDK 执行其大小与哈希规则，插件在下载完成后检查自己的 `max_file_bytes`，超过限制不发布最终目标。请同时设置 Host 文件上限和暂存空间配额。

[`docs/file-service.md:7`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L7) 说明文件能力默认关闭；34–38 行说明权限、二进制传输和 `unknown` 语义。公共 HTTP 文件通道独立于 WebUI 页面开关，不需要本插件增设 HTTP 服务或使用管理员 Token。

## 模型选择限制与当前可用的兜底

Host [`component_query.py:275`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/component_query.py#L275) 识别工具的 `visible` / `deferred` / `hidden` metadata，默认是 deferred。SDK [`components.py:415`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/maibot_sdk/components.py#L415) 的 `@Tool` 接收 metadata；官方 [`tests/test_sdk.py:78`](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/c2a039a7f4bcb367c7d770e88d6e034337be6b12/tests/test_sdk.py#L78) 已验证 `visibility="visible"` 声明。

本插件用 visible 让原聊天直接看到两个工具，但模型可能选择不调用、传错参数或仅在文本中说“已完成”。验收必须看真实工具记录与返回的 `file_ref`，不能只看自然语言回复。

可用的操作方式：

- 在原聊天明确给出工具名、JSON 参数，要求执行并报告原始结果。部署为 deferred 时，先用官方 `tool_search` 发现工具；原聊天 runtime 的 [`runtime.py:1550`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/maisaka/runtime.py#L1550) 也说明搜索只发现、不执行。
- 如需完全人工且不依赖模型，由操作者在官方 WebUI「文件」页面选择原聊天，进行文件上传/下载/发送（[`docs/file-service.md:10`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L10)）。这是用户界面操作，插件不持有管理员 Token。

原版 Host 仅在上述工具派发中签发临时文件权限，不能将 `ctx.files` 简单封装到 QQ `@Command` 或普通 API 作为确定性入口。普通插件不伪造 session、不延长后台权限。新增的可选扩展在 Host 内提供独立受限入口，不冒用上述 SDK 临时作用域。

## 可选 Host 扩展：直接交接原聊天

[`integrations/maibot_1_3_18/`](../integrations/maibot_1_3_18/) 提供人工应用、默认关闭的 Host 补丁及专用模块。它不同于安装原来的两个插件工具，不是原版 v1.3.18 已有的能力。完整配置、安装、DSH 调用及卸载步骤见[扩展 README](../integrations/maibot_1_3_18/README.md)。

新链路为 `原聊天 agent_talk → 可信 Host 任务绑定 → DSH 专用 MCP 交接 → Host FileService → 原聊天附件库`，省去处理完成后原聊天 Planner 再次选择 `dsh_import` 的步骤。它仍由 DSH 发出实际 `agent_artifact_handoff` 调用，不能保证 DSH 模型一定照做。

扩展的源码与授权设计：

1. 修改 `AgentToolProvider` 的 `agent_talk`，增加原聊天可用的 `artifact_send=false`。原版 [`messaging.py:70`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/messaging.py#L70) 区分 caller 并校验真实聊天的逻辑保持，DSH 回信不能给自己签发发送授权。
2. [`AcpAgent.enqueue():96`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L96) 创建 Host record ID 后，由 broker 将 task 绑定该记录可信 `reply_to`、Agent 身份、过期时间、发送许可和完整交接配置哈希。普通文本、DSH 参数和没有 reply_to 的 WebUI Agent 输入不能指定聊天授权。workspace、大小/数量上限、发送总开关或 TTL 与签发时不同，旧任务返回 `config_changed`，不自动续期。
3. ACP prompt 给对应任务单独附加授权块，不把原始 token 放进 Host 队列/输入 rollout。专用 provider 只在 ACP Agent 上下文可见并校验身份、token、期限及 `outbox/<task_id>/` 子目录；参数不接受聊天、任意 URL 或管理员选项。
4. Host 中的 [`artifact_provider.py`](../integrations/maibot_1_3_18/artifact_provider.py) 直接使用官方内部 [`FileService.create():177`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/service.py#L177)，传入 broker 保存的真实聊天、原文件名和受限快照字节。适配层每 broker 用 asyncio 锁串行读取快照/创建，读入内存前检查 Host 实时单文件上限。官方方法再次执行文件能力和真实聊天检查、单文件上限，并由 [`FileStore.begin_upload():175`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/store.py#L175) 预留配额、创建正式 `FileBinding`；完成时计算 SHA-256。普通插件不能照搬这种内部调用取代 `ctx.files`，Host 扩展是新增可信授权边界。
5. 发送使用 [`FileService.send():225`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/service.py#L225)，目的聊天仍取自绑定，不使用 `admin=True`。`artifact_allow_send` 与原任务 `artifact_send` 必须同时为真，DSH 才能请求发送；全局关闭而原聊天申请发送时，issue 阶段即明确返回 `send_forbidden`；`unknown` 不重发。
6. 私有 SQLite 账本先认领任务请求并持久化副作用进度；一任务一批次，同 `request_id` 同参数只查已有结果，变参数/换 ID 冲突。进程中断后的 running 状态不恢复执行，不确定结果为 unknown；这是持久 at-most-once 尝试，不是文件存储/QQ 平台的 exactly-once 事务。初始化后 key、账本或标记等组件缺失会 fail closed，不自动重建空状态；整个目录丢失或旧备份恢复仍需管理员核查副作用。

官方 [`AcpAgent._update():183`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp_agent.py#L183) 会把外部工具 rawInput 写入历史，[`acp.py:170`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/agent/acp.py#L170) 会记录 stderr；仅不保存初始 prompt 授权不足以保护凭证。启用扩展时，补丁用 `StreamRedactor` 处理 ACP 文本/思考流跨协议 chunk 的字面 token，并完全隐藏 Host 日志和错误缓存中的 stderr 内容，代价是该模式下 stderr 诊断信息减少。其他工具事件、协议错误及 Agent 回传文本中的完整 `dshh_<64hex>` 字面 token 通过匹配脱敏。主动编码/插入分隔符等变形，以及远端 DSH 自身会话持久化不在此保证内，DSH/harness 及其模型路由须可信。

安装器检查五个官方文件的固定修改前后哈希和三个新增模块，默认 `--check` 只读；仅显式 `--apply` 才改指定源码。Host 数据目录的 `artifact-handoff` key/ledger 须私有持久化，不与 DSH 共享。生产源码不会由插件自动修改，更新/卸载须部署者停机、备份、核对差异并人工执行。

## SnowLuma 的附件登记边界

[`docs/file-service.md:9`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L9) 说明统一 QQ 适配器的 NapCat/SnowLuma 画像应把文件消息和群上传通知作为结构化附件交给 Host，群上传通知还受对应设置控制。

[`src/plugin_runtime/integration.py:378`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/plugin_runtime/integration.py#L378) 根据可信 Platform IO 驱动签注 `file_source`，保存适配器 plugin、账号、平台、群/用户和原始文件定位集合，拒绝由消息负载伪造适配器身份。

[`src/files/service.py:245`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/src/files/service.py#L245) 的 `ingest()` 仅处理 `FileComponent`。没有现成 file_ref 时，需要来源 plugin_id 和 file_id，校验原始 `(file_id,busid)`、群/用户未被修改，之后才登记并回填 file_ref。普通文本中的文件名/URL不会自然变成文件库附件。

因此“QQ 文件消息显示了但 `files_list` 不存在”必须检查外部适配器、群上传通知开关、聊天过滤及 Host 登记日志。本插件不会修复适配器输出格式、补造附件来源或扫描群文件目录；没有当前聊天有效引用就不能 `dsh_export`。DSH 生成文件经本插件上传是另一条官方登记路径。

## 已验证与未验证

本机 Python 3.12、真实 PyPI SDK 2.19.0 wheel 下，指定固定 Host 源码后完整实际运行 **299 项全部通过，无失败、无跳过**。Ruff 全仓检查通过；MaiBot v1.3.18 官方 `ManifestValidator` 对 `_manifest.json` 校验通过，无 error/warning。

| 层次 | 数量 | 实际验证内容 |
| --- | ---: | --- |
| 原插件回归 | 103 | 57 项文件安全测试 + 46 项插件测试，`ctx.files` 使用受控 mock |
| 交接 core | 149 | 持久账本、任务/配置/权限绑定、文件路径/大小、重放/取消/不确定结果、流式脱敏 |
| 专用 provider | 28 | 工具可见性、Agent 上下文、Host 适配与读取前实时大小限制等受控单元测试 |
| 安装器/补丁 | 13 | 固定文件哈希、只读检查、显式应用、重复执行及拒绝覆盖；未启动完整 Host |
| Host 组件集成 | 6 | 真实 FileService / FileStore / ToolRegistry + SQLite 3 项；实际 loopback MCP 协议交接 1 项；实际补丁 ACP 方法 2 项 |

MCP 组件测试使用官方 `mcp==1.28.1` 和真实本地 MCP 服务器，经协议调用专用工具到真实 FileService 存储；ACP 方法测试执行已打补丁的 `_update` 与 stderr 处理方法。测试中的聊天发现、QQ 发送和外围生命周期使用 mock/受控替身，没有真实 QQ 投递、DSH 进程或 SSH 隧道。能证明本地专用协议入口与正式存储连通，不能推导真实 SSH/DSH/QQ 全链路成功。

Host 相关测试需要独立固定官方源码路径 `MAIBOT_SOURCE_PATH` 和 `requirements-host-test.txt`，没有源码时相关测试跳过而不是通过。CI 普通测试矩阵为 Python 3.10/3.12，Host 组件 job 为 Python 3.12。本机未运行 Python 3.10，也不以 CI 配置代替远端实际执行记录。

自动测试以临时目录与受控 `ctx.files` mock 覆盖二进制/批量/中文名、恶意路径/链接、大小和权限错误、上传/下载/发送异常及聊天访问隔离等逻辑。mock 隔离测试验证插件不选择其他聊天，不代替 Host 数据库授权、真实 Runner 临时 scope 或网络传输集成测试。

尚未在真实 MaiBot v1.3.18 + SSH ACP DSH + Linux Docker + SnowLuma QQ 环境验证：插件装载与能力授权、ACP 实际传话触发、具体模型的选工具行为、QQ 入站登记、客户端文件通道连通性、实际投递回执及部署 UID/GID。官方 [`docs/file-service.md:38`](https://github.com/Mai-with-u/MaiBot/blob/2a9af62cec37035d29237acbe2cf3adceb700b27/docs/file-service.md#L38) 也把真实 NapCat/SnowLuma 权限、地址和回执列为目标部署验收事项。

共享工作区没有聊天级文件隔离：Host 附件授权能阻止越权下载引用，但获准调用导入工具者可请求工作区中允许方向的文件，DSH 也能看到导出的文件。若租户互不信任，部署方必须分离实例和工作区。目录文件描述符会跟随 inode；即使不同 UID，只要有父目录写权限并能整体移动已打开目录，也可能将其移出工作区，因此所有有这种改名/移动能力的主体与挂载均须可信，不能把 `O_NOFOLLOW` 视为对此类攻击的隔离。实际安装及验收步骤见 [README](../README.md)。
