# PR #2：请求确认入口审阅记录

[本 PR](https://github.com/MemoryController/maibot-dsh-artifacts/pull/2) 的审核版本为 0.2.1。初审基线 `27d6c00784557128ebb9c57edf04a0a5c1382bd6` 在 main 0.2.0 `ba8a5fb4dc9857982f9cf5518b746ed0f9c282c8` 上增加 `dsh_import_request(task_id, request_sha256)`，公开 manifest v1、既有导入工具和账本格式保持兼容。提交评审不等于合并或 Release。

## 接口与权限

- 仅从固定共享路径 `outbox/.dsh-handoffs/<task_id>.json` 读取 ≤64 KiB 请求。先制作安全私有快照、验证准确原字节 SHA，再解析严格 UTF-8 JSON；拒绝重复键、非有限数、额外字段和不匹配任务标识。
- 请求直接包含 manifest v1，不包含外层任务、聊天、权限或发送包装。产物仍由现有 Handoff 按大小/哈希复验，通过 SDK `ctx.files.upload()` 登记。
- 固定 `send=False`，不接受路径、目标聊天或发送选项；不监听目录，不后台上传，不导入历史 Host 补丁，不调用内部 FileService 代替 SDK。
- task_id/SHA 只确认请求内容，不授予原聊天身份。用户须在原聊天明确确认，真实 Host scope 决定本次入库位置；kwargs、传话文字及共享 JSON 不能选择权限。
- SDK 2.19.0 没有本插件可验证的人类批准事件。“明确确认”是操作及模型策略要求，不是代码证明人类批准；本插件不解析历史消息或声称批准的文本造授权。
- 缓存/重放仅查询，经当前聊天 `ctx.files.list` 验证引用后返回最小结果；不重传、不补发，不依缓存证明原聊天投递。

审查中统一了请求暂存与产物暂存的目录校验：读取请求前检查 Host 分配的 runtime 父目录，拒绝符号链接、错误所有者及组/其他可写的暂存目录，再创建私有子目录。错误结果补齐保守的阶段字段，不把工具进入、冲突或参数失败误说成已经登记/发送。

## 双端固定源码与版本

| 组件 | 固定审查来源 |
| --- | --- |
| Host | MaiBot v1.3.18，`2a9af62cec37035d29237acbe2cf3adceb700b27` |
| SDK | v2.19.0，`c2a039a7f4bcb367c7d770e88d6e034337be6b12` |
| DSH 插件历史 0.2.0 | `0958d8ac788ebbea5f2c507f3b024de3dd06cb24` |
| DSH 插件当前 main 0.2.1 | `f008cdc039122d360dee67b86e1e2004083ba019` |

DSH 插件包与 DSH CLI 版本独立。两版插件均声明 Node ≥24、Linux、DSH runtime 0.2.2-alpha.2；核查远端没有 tags，不以 main 或 package.version 宣称 Release。仅修改本仓库文档，不代为改写 DSH 仓库。DSH 当前 main 默认 manifest 模式兼容对端 0.2.0/本 PR；显式 request 才要求本 PR。历史 0.2.0 的默认请求协议可读，但发布后私有记录失败的状态缺陷已在当前 DSH main 修复，不推荐回滚处理未知任务。完整矩阵见[兼容说明](compatibility.md)。

## 验证分层与边界

| 层次 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| Python 单元/组件测试 | 实际请求读取、私有快照、严格解析、SQLite claim/重放、取消/错误、作用域 double 权限逻辑 | 真实 Host 文件 scope 或生产附件 |
| 真实 SDK 声明/临时安装 | SDK 能解析工具声明，在无 Host 源码临时 plugins 目录加载完整插件 | 真实 Runner 全生命周期或 Docker 挂载 |
| 原版 ManifestValidator | 0.2.1 manifest 符合固定 Host 规则 | ACP 路由、上传 HTTP 或 QQ 投递 |
| 固定 DSH 编译产物跨语言检查 | DSH 真实生成的请求原字节可被本仓库正式 request/Handoff 内核校验；确认模式/阶段/去重语义一致 | 真实 DSH 对话、SSH、Host scope 和 SnowLuma |

Python 测试 Files API 明确使用聊天作用域 double，其引用只是测试数据。只有生产 Host 工具调用的官方 upload 返回 file_ref，才是生产登记证据。`submitted` 只表示 DSH 共享发布，`queued` 只表示传话排队；本地 status 不读取 MaiBot 库，不用共享 receipt JSON 自证入库。unknown/unconfirmed 均禁止自动重做。

本次 Python 3.12 正式套件 **303 passed，无失败、无跳过**；Ruff/diff 检查通过。原版未修改的 MaiBot v1.3.18 ManifestValidator 复验 0.2.1 manifest，无 errors/warnings。初审 286 仅是历史基线。

独立 `protocol_checks/dsh_protocol.py` 使用上述两版固定 DSH dist，各 **6 passed，共 12**。它实际运行 DSH ArtifactService/文件系统和 Python request/Handoff/SQLite；仅 Files API 为作用域 double。覆盖四文件（中文/emoji/空/二进制）、准确原字节、默认模式、重复与重启、跨 scope、回执丢失、submitted 尚未入库和字节篡改，不启动真实 ACP/DSH 对话。

正式 303 与独立协议 12 分开报告。CI 已固定双 SHA、Node 24/Python 3.12 矩阵，配置不是远端已通过的证据；复现命令和分层见[兼容说明](compatibility.md#模型选择外部依赖与验证)。固定源码的通过不扩展成未经测试版本兼容声明。本次提交用于将 PR 推进到可审阅状态，不表示合并或发布。

**尚未验收完整真实 MaiBot + Docker + SSH ACP + DSH + SnowLuma QQ。** 目标部署仍需实际工具调用、同聊天 files_list、文件字节/哈希及平台回执；不要把准备、排队、工具被选择或测试引用当作生产入库成功。
