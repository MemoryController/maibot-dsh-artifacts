# 隔离的官方 Host 文件组件集成检查

这是单独运行的测试，不属于默认模拟套件，也**不是真实聊天/QQ 端到端测试**。测试不会安装或运行 `integrations/` 的历史补丁，不启动生产 MaiBot，不读取其配置、数据库或凭据。

实际执行的代码：

- 未修改的 MaiBot v1.3.18 `2a9af62cec37035d29237acbe2cf3adceb700b27` 的 FileService、FileStore、file_capability、HTTP router、FileToolProvider。
- 官方 SDK v2.19.0 的上传/下载，与测试回环 HTTP 端点传输真实字节。
- 当前 DSH 配套包 v0.2.1 `f008cdc039122d360dee67b86e1e2004083ba019` 的真实 dist prepare/publish。
- 本插件入口、安全快照和持久账本；Host 文件登记使用临时 SQLite 和文件盘。

测试替代边界：聊天注册表、配置入口、日志启动、QQ transport，以及 SDK 到 capability 的 RPC 桥。文件 scope 由测试夹具使用真实 FileService 签发/撤销，**没有执行真正的 component_query/Runner 派发链**。测试票据绝不进入产品代码。QQ transport 返回 unknown 的测试只能验证 Host/插件对该结果的处理，不能证明 QQ 投递。

检查成功说明临时库中已产生官方 FileStore 的实际 file_ref、可用内置 files_list 查询、官方下载字节与原文一致、跨 scope 拒绝，以及缓存/回执丢失/取消后不重做。除代表性 Pillow 消费者外，新增检查实际运行 dsh_open_image 和官方媒体转换/解析方法；**没有安装下游图片编辑插件**。附件 file_ref 不等于聊天图片消息 ID。

## 运行

需要 Linux、Python 3.12、Node ≥24。使用独立临时 checkout，不能指向生产 MaiBot；测试检查固定 SHA 和受测文件没有修改，不向官方 checkout 写字节码。回环 HTTP 服务只用于测试，正式插件没有新增服务。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r host_checks/requirements.txt

git clone --no-checkout https://github.com/Mai-with-u/MaiBot.git /tmp/stock-maibot-test
git -C /tmp/stock-maibot-test checkout --detach 2a9af62cec37035d29237acbe2cf3adceb700b27
git clone --no-checkout https://github.com/MemoryController/dsh-maibot-artifacts.git /tmp/stock-dsh-test
git -C /tmp/stock-dsh-test checkout --detach f008cdc039122d360dee67b86e1e2004083ba019

MAIBOT_SOURCE_PATH=/tmp/stock-maibot-test \
DSH_ARTIFACTS_SOURCE=/tmp/stock-dsh-test \
.venv/bin/python -m pytest -q host_checks/stock_files.py
```

这些路径仅为测试 checkout 示例。测试使用 OS 分配的临时目录和 127.0.0.1 随机端口；不需要管理员 Token、Docker Socket 或 root。CI 有独立 `stock-host-components` job。

真实联合验收仍需用户的独立测试环境观察真实工具派发、两次模型决策、原任务路由、生产格式的 file_ref、files_list、目标图片工具与 SnowLuma QQ 发送；见[免复制流程](../docs/automation.md)。

## 官方媒体源方法组件检查

媒体检查实际运行本插件的 `dsh_open_image`，再通过官方转换代码检查：

1. 插件结果由官方 `InvokeResultPayload`/`Envelope` 和 `MsgPackCodec` 编码、解码，检查样例帧不超过官方 16 MiB 上限，再将 `content_items` 转为真实 `ToolContentItem`；这不是实际 IPC 连接或最大尺寸压力测试。
2. 媒体字节进入真实 `ImageComponent` 和 `ToolResultMediaMessage`，Host 规则生成 `tool_result:<测试调用ID>:1`。
3. 原版 `to_context_item` 在视觉输入开启时生成真实 `ContextImagePart`；关闭时不产生图片片段。
4. 官方 SDK `ctx.maisaka.context.resolve_image` 调用完整官方能力处理方法和图片解析器，从已放入测试历史的媒体索引读回原始字节。

生成媒体保留 `source_kind=tool_result_media`，不属于 `SessionBackedMessage`，不计作真实 IM 消息，也没有伪造用户信息或原始用户消息。

为避免完整 Planner 导入时启动大量无关服务，`media_methods.py` 从固定官方源码抽取 ComponentQueryService 的结果转换方法与 MaisakaReasoningEngine 的媒体方法，**保留方法体、装饰器和参数不变**后编译；同时检查方法签名。相关真实数据模型、上下文投影和图片解析器直接导入，未使用历史 Host 补丁。未使用的 SessionMessage 类被替代；测试历史由夹具保存。因此这部分准确名称是“官方媒体源方法组件测试”，不能称为完整 Host 模块、Planner 或工具派发集成。

覆盖新的图片 MIME、历史 `application/octet-stream` 图片、引用隔离、权限失效、不重复上传、回执丢失、取消后的安全重查，以及发送 unknown 不补发。媒体解析器按指定的现存上下文查询；它不是文件库授权 API，本插件未申请该能力。读取附件的授权仍由当前 Host 文件 scope 提供。

**这些检查不是实际 Docker + SSH ACP + DSH + SnowLuma + QQ 端到端测试，也没有安装或运行特定第三方图片编辑插件。** 它们验证受支持的媒体索引交接契约；实际图片插件是否接受该索引、模型是否会选择它，以及生产环境能否发送图片，仍需分别验收。
