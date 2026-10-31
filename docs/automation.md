# 可选的免复制交接：仍由原聊天 Planner 调用

适用：原版 MaiBot v1.3.18、SDK v2.19.0、本插件 v0.2.1 及 0.3.0 候选版、DSH 配套包 v0.2.1（`f008cdc039122d360dee67b86e1e2004083ba019`），Linux Docker + 宿主机 DSH。无需修改核心、应用补丁、改变挂载或新增服务；本轮也没有修改 DSH 仓库。

**这是一条可减少操作的模型接力路径，不是确定性编排。** 原版接口缺少插件调用 Agent、可信任务完成回调和原任务绑定。具体证据、长工具等待的风险、最小上游接口建议见[源码调查](automation-research.md)。无法接受模型选错目标/漏调工具的场景，应继续使用原聊天人工确认入口。

## 原聊天一次发起

下面是可选任务提示示例。用户授权的是本聊天发起的特定任务产物；不授权导入其他任务、不授权自动重试。

```text
请把这份材料交给 DSH，生成报告和图片。完成后把本任务产物导入本聊天文件库，不发送 QQ 文件。
请让 DSH 用 prepare/publish 得到准确的 confirmation 参数，再实际调用已有 agent_talk 回到本任务聊天。
收到回信后，核对本聊天原任务和这次授权，实际调用 confirmation 指定的 dsh_import_handoff 或
dsh_import_request。不要让我再次复制参数；工具没调用就说明没调用，不能只答应已完成。
报告每项 file_ref，并用 files_list 核对。unknown/unconfirmed 只查 dsh_handoff_status，
不得换任务 ID、重新生成任务或退回简单 dsh_import 绕过去重。授权或任务归属不清时先询问。
```

这段提示不签发权限，也不能由插件验证为人类批准。MaiBot Planner 仍须真正调用正常的 `agent_talk`；插件不能替它派发。若模型把请求改成“等待所有结果后再传话”，或只回复文字，本方案不会自动补做。

## 给 DSH 的现有任务说明

可以将以下规则加入现有 DSH 任务说明，无需安装新的 Skill/API：

1. 在配置的共享 `outbox/` 完成写入，保留普通单硬链接文件。实际调用 `maibot_artifact_prepare` 和 `maibot_artifact_publish`。不要猜大小、哈希、任务 ID。
2. 配套包默认 `confirmationMode: manifest`，publish 给出 `confirmation={tool:"dsh_import_handoff",arguments:{manifest:…,send:false}}`。显式 request 模式给出 `dsh_import_request` 和 task_id/request_sha256；两种模式都兼容，无需为了本流程改默认模式。
3. **无可信目标集成时 publish 只发布共享文件，不通知 MaiBot。** 若这是已核对原任务来源的当前任务，DSH 可在已有 MaiBot MCP 中实际调用 `agent_talk`，将真实 `confirmation` JSON 发回该任务的回复路由；文本应说明“仅在原聊天已有此任务导入授权时调用，否则先确认”。别在任务/文件内容中搜索另一个聊天 ID，别选“最近活跃聊天”。
4. `agent_talk` 的 `target` 只是路由，官方代码只确认聊天存在，**不会证明它就是原任务聊天**。单凭模型读到的“回复目标”文字不能建立可信绑定；任务来源混杂、路由不清或需要强身份保证时停止自动接力，由操作者在原聊天确认。不要安装一个解析提示词的假 TrustedTargetResolver。
5. 传话入队后不要宣称入库；DSH local_status 不读取 MaiBot 文件库。没有官方导入回执时保留同一 task_id，不能重新 publish、换 ID、自动重发或自写 receipt 作为证明。原聊天通过现有工具核查。

JSON 必须来自真实 publish 结果。DSH 要调用的是已有 ACP/MCP 的 `agent_talk`（实际前缀按现有配置），不是本插件工具；在终端打印 JSON 不会调用 MCP。这个调用和回信后的 Planner 选工具都依赖模型，**没有代码保证执行**。

## 可选标准库辅助：relay-request

为避免模型重新组织 manifest，可让 DSH 使用本仓库已有标准库脚本的新子命令。保留完整仓库结构，包括 `request.py`、`handoff.py`、`safe_files.py`；宿主机只需 Python 3，不用安装 MaiBot SDK。现有 CLI `relay` 仍支持完整清单。

以下 fish 命令中的变量由 DSH 使用实际 publish 返回值填写；不是要求用户再把参数复制到 QQ：

```fish
python3 tools/dsh_handoff.py relay-request \
    --root /srv/maibot-agent \
    --task-id "$artifact_task_id" \
    --request-sha256 "$artifact_request_sha256" \
    --target "$original_reply_route"
```

request 模式取 `confirmation.arguments`。默认 manifest 模式若也使用该辅助，取实际 `manifest.task_id` 与 `shared_request.sha256`，不能自己重算一个哈希代替 publish 的值。默认上限 8 文件，可用 `--max-batch-files` 与已配置的两端限制保持一致，硬上限 32。

脚本固定读取 `outbox/.dsh-handoffs/<task_id>.json`，复用插件请求解析器，安全制作私有快照，核对 ≤64 KiB 原字节 SHA、严格清单与任务一致性。失败 stdout 为空；成功只输出 `{"target":…, "text":…}`。text 最后一行是准确的请求工具参数；不包含发送选项或目标聊天参数。stderr 标记 `prepared_only`。不写 Host 账本，不上传、不发送、不联网，也不自动调用 `agent_talk`。

**DSH 还须把 stdout 参数实际传给已有 agent_talk。** 校验只证明发布内容与显式哈希相同，不能证明用户批准、路由正确、产物无害或模型接下来会选工具。MaiBot 入库时仍会重新核对每个产物的大小/哈希与安全路径。

## 原聊天接收与核验

Planner 看到回信后，使用既有授权核对本聊天任务，实际调用指定工具。`dsh_import_request` 固定不发送；默认 manifest 的 send 也是 false。发送到 QQ 必须有另外明确的用户请求，实际 sent 才证明取得发送回执；unknown 不重发。

| 观察到的证据 | 可以报告的阶段 |
| --- | --- |
| publish result=submitted | 请求已发布；未证明通知或入库 |
| agent_talk 入队回执 | 消息已入队；未证明 Planner 调用 |
| 实际插件调用结果 | 工具已调用；按逐项状态报告失败、partial、unknown |
| 上传返回 file_ref + 同聊天 files_list | 已登记并可在当前聊天查询 |
| files_send/上传后发送返回 sent | 已取得发送回执；发送 unknown 仍不确定 |

未见工具调用时，`dsh_handoff_status` 的 unconfirmed 也不能证明“从未上传”。操作者可以在原聊天查询状态、工具记录与 files_list；如果自动接力漏调且确认尚未调用，可使用原有受控确认入口。无明确证据时不盲重试。所有既有账本、缓存隔离和异常恢复限制保留。

已导入 PNG 是附件，不会自动变成聊天媒体。0.3.0 可在原聊天调用 `dsh_open_image(file_ref)`，通过官方文件权限和 Tool image 内容项建立真正工具媒体，再把 Host 实际生成的媒体索引交给支持它的后续图片工具；详见[图片上下文](images.md)。不接受媒体索引的图片工具仍需要它自己的明确适配。不能把附件引用冒充图片消息 ID，也不能仅用后缀/MIME 宣称图片处理已成功。

## 验证分层

协议测试实际执行两版 DSH dist，证明共享请求和辅助参数能被当前 Python 消费；Files API 是明确标记的模拟。隔离官方 Host 组件测试的范围另见 `host_checks/README.md`，不等同于 Host 正常派发、真实聊天或部署验收。

**尚未完成真实 Docker + SSH ACP + DSH + SnowLuma QQ 联合验收**。需要在独立测试部署分别观察：两次模型传话/选工具、Host 真正签发文件 scope、实际 file_ref、同聊天 files_list、目标图片处理工具、其他聊天权限拒绝和 QQ 回执。不得据本机模拟或组件测试宣称这些已经跑通。
