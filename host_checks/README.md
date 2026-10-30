# 隔离的官方 Host 文件组件集成检查

这是单独运行的测试，不属于默认模拟套件，也**不是真实聊天/QQ 端到端测试**。测试不会安装或运行 `integrations/` 的历史补丁，不启动生产 MaiBot，不读取其配置、数据库或凭据。

实际执行的代码：

- 未修改的 MaiBot v1.3.18 `2a9af62cec37035d29237acbe2cf3adceb700b27` 的 FileService、FileStore、file_capability、HTTP router、FileToolProvider。
- 官方 SDK v2.19.0 的上传/下载，与测试回环 HTTP 端点传输真实字节。
- 当前 DSH 配套包 v0.2.1 `f008cdc039122d360dee67b86e1e2004083ba019` 的真实 dist prepare/publish。
- 本插件入口、安全快照和持久账本；Host 文件登记使用临时 SQLite 和文件盘。

测试替代边界：聊天注册表、配置入口、日志启动、QQ transport，以及 SDK 到 capability 的 RPC 桥。文件 scope 由测试夹具使用真实 FileService 签发/撤销，**没有执行真正的 component_query/Runner 派发链**。测试票据绝不进入产品代码。QQ transport 返回 unknown 的测试只能验证 Host/插件对该结果的处理，不能证明 QQ 投递。

检查成功说明临时库中已产生官方 FileStore 的实际 file_ref、可用内置 files_list 查询、官方下载字节与原文一致、跨 scope 拒绝，以及缓存/回执丢失/取消后不重做。PNG 由代表性 Pillow 测试消费者处理，**不是已安装的下游图片工具**；附件 file_ref 不等于聊天图片消息 ID。

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
